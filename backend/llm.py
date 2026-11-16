"""One structured-output call, two providers.

    LLM_PROVIDER=openrouter (default)  OpenAI-compatible chat completions on OpenRouter,
                                       response_format=json_schema (strict). Key: OPENROUTER_API_KEY.
                                       Model: LLM_MODEL (default openai/gpt-4o-mini).
    LLM_PROVIDER=anthropic             Claude Messages API, output_config.format json_schema.
                                       Key: ANTHROPIC_API_KEY. Model: LLM_MODEL (default claude-opus-5-5).

Both return (parsed_json, usage). Errors are mapped to LLMAuthError / LLMModelError so
callers can disable enrichment cleanly instead of retrying forever.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROVIDER = os.environ.get("LLM_PROVIDER", "openrouter").lower()
DEFAULT_MODELS = {"openrouter": "openai/gpt-4o-mini", "anthropic": "claude-opus-5-5"}
MODEL = os.environ.get("LLM_MODEL") or os.environ.get("ENRICH_MODEL") or DEFAULT_MODELS.get(PROVIDER, "")
OPENROUTER_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Provider routing on OpenRouter: "throughput" measured ~2.9s vs ~3.6-5.3s default for gpt-4o-mini
# cards (it currently routes to Azure-hosted OpenAI). Empty string = OpenRouter default routing.
OPENROUTER_SORT = os.environ.get("OPENROUTER_PROVIDER_SORT", "throughput")
ANTHROPIC_EFFORT = os.environ.get("ENRICH_EFFORT", "low")
ANTHROPIC_FALLBACKS = os.environ.get("ENRICH_FALLBACKS", "1") not in ("0", "false", "no")


class LLMAuthError(Exception):
    pass


class LLMModelError(Exception):
    pass


class LLMRefusal(Exception):
    pass


def credentials_configured() -> bool:
    """Presence check only, values are never read."""
    if PROVIDER == "openrouter":
        return bool(os.environ.get("OPENROUTER_API_KEY"))
    if any(os.environ.get(v) for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")):
        return True
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anthropic"
    return cfg.is_dir() and any(cfg.iterdir())


def missing_credentials_hint() -> str:
    var = "OPENROUTER_API_KEY" if PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
    return f"no {PROVIDER} credentials (set {var}); showing rule-based cards"


class StructuredLLM:
    def __init__(self) -> None:
        self.provider = PROVIDER
        self.model = MODEL
        self._client = None
        self._fallbacks = ANTHROPIC_FALLBACKS

    def describe(self) -> dict:
        return {"provider": self.provider, "model": self.model,
                **({"routing": OPENROUTER_SORT or "default"} if self.provider == "openrouter" else {}),
                **({"effort": ANTHROPIC_EFFORT} if self.provider == "anthropic" else {})}

    async def warm(self) -> None:
        """Open the HTTPS connection ahead of a call (no tokens). Best effort, OpenRouter only."""
        if self.provider != "openrouter" or not credentials_configured():
            return
        try:
            await self._openai().get("/key", cast_to=object)
        except Exception:
            pass

    def _openai(self):
        import openai

        if self._client is None:
            self._client = openai.AsyncOpenAI(base_url=OPENROUTER_BASE, api_key=os.environ.get("OPENROUTER_API_KEY"),
                                              max_retries=2, timeout=60.0)
        return self._client

    async def structured(self, system: str, user: str, schema: dict, name: str, max_tokens: int = 4000) -> tuple[dict, dict]:
        if self.provider == "openrouter":
            return await self._openrouter(system, user, schema, name, max_tokens)
        if self.provider == "anthropic":
            return await self._anthropic(system, user, schema, max_tokens)
        raise LLMModelError(f"unknown LLM_PROVIDER {self.provider!r}")

    # --- OpenRouter (OpenAI-compatible) ---------------------------------------------
    async def _openrouter(self, system, user, schema, name, max_tokens):
        import openai

        client = self._openai()
        try:
            r = await client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
                extra_body={"provider": {"sort": OPENROUTER_SORT, "require_parameters": True}} if OPENROUTER_SORT else None,
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
            raise LLMAuthError(f"{type(e).__name__}: {str(e)[:160]}") from e
        except openai.NotFoundError as e:
            raise LLMModelError(f"model {self.model} not available: {str(e)[:160]}") from e
        choice = r.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError("structured output truncated at max_tokens")
        if getattr(choice.message, "refusal", None):
            raise LLMRefusal(choice.message.refusal)
        usage = {"input_tokens": r.usage.prompt_tokens if r.usage else None,
                 "output_tokens": r.usage.completion_tokens if r.usage else None, "served_by": r.model,
                 "upstream": (r.model_extra or {}).get("provider")}
        return json.loads(choice.message.content), usage

    # --- Anthropic ---------------------------------------------------------------------
    async def _anthropic(self, system, user, schema, max_tokens):
        import anthropic

        if self._client is None:
            self._client = anthropic.AsyncAnthropic(max_retries=2, timeout=60.0)
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": ANTHROPIC_EFFORT, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            if self._fallbacks:
                try:
                    resp = await self._client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"],
                                                                   fallbacks="default")
                except anthropic.BadRequestError as e:
                    if "fallback" not in str(e).lower():
                        raise
                    self._fallbacks = False
                    resp = await self._client.messages.create(**kwargs)
            else:
                resp = await self._client.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise LLMAuthError(f"{type(e).__name__}: {str(e)[:160]}") from e
        except anthropic.NotFoundError as e:
            raise LLMModelError(f"model {self.model} not available: {str(e)[:160]}") from e
        except TypeError as e:  # SDK could not resolve any credentials
            if "authentication" in str(e).lower():
                raise LLMAuthError(str(e)[:160]) from e
            raise
        if resp.stop_reason == "refusal":
            raise LLMRefusal(str(getattr(resp, "stop_details", "")))
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("structured output truncated at max_tokens")
        text = next(b.text for b in resp.content if b.type == "text")
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens, "served_by": resp.model}
        return json.loads(text), usage
