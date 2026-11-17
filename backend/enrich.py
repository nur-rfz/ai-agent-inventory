"""LLM enrichment: evidence bundle -> AgentCard (structured output).

- Input is the redacted, compact evidence summary built by resolve.evidence_summary();
  each item carries an id (E1, E2, ...) that the card must cite, mapped back to
  signal ids so every field in the UI is clickable evidence.
- Output is constrained by a strict JSON schema (backend/llm.py: OpenRouter
  `response_format=json_schema` by default, or Anthropic `output_config.format`).
- Cached by (evidence hash, provider/model, prompt version) in SQLite: re-seeding or
  restarting never re-calls the API for unchanged evidence.

Env: LLM_PROVIDER (openrouter | anthropic), LLM_MODEL, OPENROUTER_API_KEY / ANTHROPIC_API_KEY,
ENRICH_ENABLED, ENRICH_CONCURRENCY.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

from sqlalchemy import Column
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel

from common.redact import scrub
from common.schema import now_iso

from . import llm
from .db import session

log = logging.getLogger("enrich")

PROMPT_VERSION = "v4"
ENABLED = os.environ.get("ENRICH_ENABLED", "1") not in ("0", "false", "no")
CONCURRENCY = int(os.environ.get("ENRICH_CONCURRENCY", "4"))
HOT_WORKERS = 2  # live discoveries (e.g. your GitHub + the new server) never queue behind each other
MODEL_ID = f"{llm.PROVIDER}:{llm.MODEL}"
REVIEW_THRESHOLD = 0.6
IDENTITY_ONLY_MAX_CONFIDENCE = 0.5  # below the review threshold: a human confirms what these SaaS apps do


class EnrichmentCache(SQLModel, table=True):
    key: str = Field(primary_key=True)
    agent_id: str = Field(index=True)
    evidence_hash: str
    model: str
    card: dict = Field(sa_column=Column(JSON))
    created_at: str
    duration_ms: int
    usage: dict = Field(sa_column=Column(JSON))


def cache_key(evidence_hash: str, model: str = MODEL_ID) -> str:
    return f"{evidence_hash}|{model}|{PROMPT_VERSION}"


_STR = {"type": "string"}
_IDS = {"type": "array", "items": {"type": "string"}}
CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "job", "purpose", "purpose_rationale", "purpose_evidence", "capabilities", "data_reach",
                 "triggers", "classification", "classification_rationale", "instructions_summary",
                 "confidence", "confidence_rationale"],
    "properties": {
        "name": _STR,
        "job": _STR,
        "purpose": _STR,
        "purpose_rationale": _STR,
        "purpose_evidence": _IDS,
        "capabilities": {"type": "array", "items": _STR},
        "data_reach": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["system", "access", "rationale", "evidence"],
            "properties": {"system": _STR, "access": {"type": "string", "enum": ["r", "w", "rw"]},
                           "rationale": _STR, "evidence": _IDS}}},
        "triggers": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["type", "description", "cron", "evidence"],
            "properties": {"type": {"type": "string", "enum": ["schedule", "manual", "event", "unknown"]},
                           "description": _STR, "cron": _STR, "evidence": _IDS}}},
        "classification": {"type": "string", "enum": ["replace", "govern_in_place", "ok"]},
        "classification_rationale": _STR,
        "instructions_summary": _STR,
        "confidence": {"type": "number"},
        "confidence_rationale": _STR,
    },
}

SYSTEM_PROMPT = """You analyze AI agents that employees built on their own ("shadow AI") so IT can inventory them and consolidate duplicates into shared agents on Langdock, the company's sanctioned AI platform.

You get an evidence bundle for ONE agent, gathered by endpoint sensors and identity logs. Secrets are already redacted; names of environment variables and headers remain. Each evidence item has an id like "E3". Produce an AgentCard.

How to fill each field:
- name: 2-5 words naming what the agent DOES for the business, not the tool it runs in ("Customer email summarizer", "Weekly pipeline report", "Billing-service coding assistant"). No person names.
- job: the core recurring job in 2-5 lowercase, vendor-neutral words, with no product, system or team names ("summarize customer emails", "weekly sales pipeline report", "review pull requests", "answer questions on documents"). Two agents built by different people for the same job must get the same job text even if they use different tools (Gmail vs Outlook, script vs chat app).
- purpose: one sentence: what it does, for whom, with which systems. Say what is inferred vs. visible when that matters.
- purpose_rationale: one short sentence on which evidence decided it. purpose_evidence: the ids you relied on.
- capabilities: 3-7 short verb phrases a replacement must cover ("read unread customer emails", "summarize each thread", "look up the account in CRM", "post a digest to Slack"). Base them on tools, code and prompts. These are used to find duplicate agents built by different people, so phrase them in plain, vendor-neutral terms where possible ("read inbox", not "call list-mail-messages").
- data_reach: the business systems it can touch. access: r (only reads), w (only writes), rw. Prefer the systems already listed in data_reach evidence; you may add a system only if code, prompts or tool descriptions clearly show it (e.g. saves a file to OneDrive/SharePoint -> Microsoft 365 w). Do not list LLM providers or identity providers here. Cite evidence ids.
- triggers: how it runs. schedule (cron/launchd; copy the cron expression), manual (a person uses it interactively, describe the observed pattern), event (reacts to something), unknown. cron is "" unless it is a schedule. A schedule mentioned only in server instructions or a docstring is still a schedule; cite where.
- classification:
  * replace: a business automation Langdock can host: email triage/summaries, reports, document Q&A, scheduled summaries, CRM lookups, notifications.
  * govern_in_place: coding agents (Cursor, Claude Code, VS Code) or third-party SaaS products Langdock cannot reproduce (e.g. meeting recorders): keep, inventory, manage risk.
  * ok: already sanctioned (the sanctioned app itself).
  The bundle includes a rule-based suggestion; keep it unless the evidence clearly says otherwise, and explain.
- instructions_summary: what the agent's instructions/prompt are or appear to be, in 1-3 sentences. Quote visible prompts closely; otherwise write "Inferred: ..." from tools and behavior.
- confidence: 0.0-1.0 that name/purpose/classification are right. Below 0.6 means a human should review (e.g. no tools visible, server could not be probed, generic names). confidence_rationale: one sentence.

When no prompt or instructions are visible, infer the most likely recurring business workflow from the combination of the owner's role, the systems, what it writes, the tool descriptions and the usage pattern, and name/describe/phrase capabilities as that workflow. Never just restate tool names (not "search emails, read email, send email", but "triage the support inbox", "summarize customer threads", "draft replies"). Lower confidence accordingly.

SaaS apps known only from identity logs (an OAuth consent, no tools or code): describe what the product itself does (e.g. "ChatGPT with SharePoint file access", "Meeting recorder and notes"), do not guess a business workflow, and keep confidence at 0.6 or below.

Use only the evidence. Do not invent tools, systems, schedules or people. Keep every text field short."""


def _user_message(agent: dict, policy: dict) -> str:
    bundle = {
        "agent": {"surface": agent["surface"], "domain": agent["domain"], "owner": agent["_evidence"]["owner"],
                  "rule_based_classification": agent["classification"],
                  "rule_based_reason": agent["rationale"].get("classification")},
        "company_policy": policy,
        "evidence": agent["_evidence"]["evidence"],
    }
    return "Evidence bundle:\n" + json.dumps(scrub(bundle), indent=1, default=str)


class Enricher:
    """Background worker: one Claude call per (agent, evidence hash) not yet in the cache."""

    def __init__(self, on_card: Callable[[str], Awaitable[None]], policy: dict):
        self.on_card = on_card
        self.policy = policy
        self.cards: dict[str, dict] = {}
        self.inflight: set[str] = set()
        self.failed: dict[str, float] = {}  # key -> monotonic time of last failure (60s back-off)
        # newest request first: the agent that just appeared on camera jumps the backlog
        self.queue: asyncio.PriorityQueue[tuple[float, int, dict]] = asyncio.PriorityQueue()
        self._seq = 0
        # fast lane: agents that just appeared (live on camera) never wait behind a backlog
        self.hot: asyncio.Queue[dict] = asyncio.Queue()
        self.disabled_reason: str | None = None if ENABLED else "ENRICH_ENABLED=0"
        if not self.disabled_reason and not llm.credentials_configured():
            self.disabled_reason = llm.missing_credentials_hint()
        self.llm = llm.StructuredLLM()
        self.stats = {"calls": 0, "cache_hits": 0, "errors": 0, "last_error": None, "last_ms": None}
        self._tasks: list[asyncio.Task] = []
        with session() as s:
            for row in s.query(EnrichmentCache).all():
                self.cards[row.key] = {**row.card, "_meta": {"model": row.model, "duration_ms": row.duration_ms,
                                                             "created_at": row.created_at, "cached": True}}

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(CONCURRENCY)]
        self._tasks += [asyncio.create_task(self._worker(hot=True)) for _ in range(HOT_WORKERS)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    def warm(self) -> None:
        """A new agent just appeared: pre-open the LLM connection while its probe runs."""
        if not self.disabled_reason and time.monotonic() - getattr(self, "_warmed", -1e9) > 10:
            self._warmed = time.monotonic()
            asyncio.create_task(self.llm.warm())

    def card_for(self, agent: dict) -> dict | None:
        return self.cards.get(cache_key(agent["evidence_hash"]))

    def request(self, agent: dict, hot: bool = False) -> None:
        key = cache_key(agent["evidence_hash"])
        if self.disabled_reason or key in self.cards or key in self.inflight:
            return
        if time.monotonic() - self.failed.get(key, -1e9) < 60:
            return
        self.inflight.add(key)
        self._seq += 1
        job = {"key": key, "agent": agent}
        if hot:
            self.hot.put_nowait(job)
        else:
            self.queue.put_nowait((-time.monotonic(), self._seq, job))

    def status(self) -> dict:
        return {"enabled": self.disabled_reason is None, "disabled_reason": self.disabled_reason,
                **self.llm.describe(), "prompt_version": PROMPT_VERSION, "queued": self.queue.qsize() + self.hot.qsize(),
                "inflight": len(self.inflight), "cached_cards": len(self.cards), **self.stats}

    async def _worker(self, hot: bool = False) -> None:
        while True:
            job = await self.hot.get() if hot else (await self.queue.get())[2]
            try:
                if not self.disabled_reason:
                    await self._enrich(job["key"], job["agent"])
            except Exception as e:  # never kill the worker
                log.warning("enrichment failed: %s", e)
                self.failed[job["key"]] = time.monotonic()
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            finally:
                self.inflight.discard(job["key"])
                await self.on_card(job["agent"]["id"])  # refresh status either way

    async def call_llm(self, agent: dict) -> tuple[dict, dict]:
        return await self.llm.structured(SYSTEM_PROMPT, _user_message(agent, self.policy), CARD_SCHEMA, "agent_card")

    async def _enrich(self, key: str, agent: dict) -> None:
        t0 = time.monotonic()
        log.info("llm call start %s", agent["id"])
        try:
            card, usage = await self.call_llm(agent)
        except llm.LLMAuthError as e:
            self.disabled_reason = f"{self.llm.provider} rejected the credentials ({e}); showing rule-based cards"
            log.warning(self.disabled_reason)
            return
        except llm.LLMModelError as e:
            self.disabled_reason = str(e)
            log.warning(self.disabled_reason)
            return
        ms = int((time.monotonic() - t0) * 1000)
        log.info("card ready %s in %d ms", agent["id"], ms)
        self.stats["calls"] += 1
        self.stats["last_ms"] = ms
        card = scrub(card)
        with session() as s:
            s.merge(EnrichmentCache(key=key, agent_id=agent["id"], evidence_hash=agent["evidence_hash"], model=MODEL_ID,
                                    card=card, created_at=now_iso(), duration_ms=ms, usage=usage))
            s.commit()
        self.cards[key] = {**card, "_meta": {"model": MODEL_ID, "duration_ms": ms, "created_at": now_iso(),
                                             "cached": False, "usage": usage}}


def apply_card(agent: dict, card: dict | None, enrich_status: dict) -> dict:
    """Merge an LLM card into a rule-based agent. Evidence-backed facts are never removed."""
    if not card:
        agent["status"] = "provisional" if enrich_status.get("enabled") else "rules_only"
        return agent
    eid_map = {e["eid"]: e["signal_ids"] for e in agent["_evidence"]["evidence"]}

    def ids(eids: list[str]) -> list[str]:
        return sorted({sid for e in eids for sid in eid_map.get(e, [])})

    agent["name"] = card["name"]
    agent["purpose"] = card["purpose"]
    agent["capabilities"] = card["capabilities"]
    agent["job"] = card.get("job")
    agent["instructions_summary"] = card["instructions_summary"]
    agent["confidence"] = round(float(card["confidence"]), 2)
    if agent["surface"] == "saas" and agent["classification"] != "ok":
        # known only from an OAuth consent / key event: the model can't see what it does
        agent["confidence"] = min(agent["confidence"], IDENTITY_ONLY_MAX_CONFIDENCE)
    agent["rationale"] = {
        "purpose": card["purpose_rationale"], "purpose_evidence_ids": ids(card["purpose_evidence"]),
        "classification": card["classification_rationale"], "confidence": card["confidence_rationale"],
        "rules_classification": agent["rationale"].get("classification"),
    }
    if agent["classification"] != "ok":  # policy-sanctioned stays ok
        agent["classification"] = card["classification"]
        agent["classification_source"] = "llm"
    by_sys = {r["system"]: r for r in agent["data_reach"]}
    for r in card["data_reach"]:
        if r["system"] in by_sys:
            cur = by_sys[r["system"]]
            cur["rationale"] = r["rationale"]
            if set(r["access"]) - set(cur["access"]):  # the LLM saw broader access in code/prompts
                cur["access"] = "rw"
                cur["source"] = "evidence+llm"
                cur["evidence_ids"] = sorted(set(cur["evidence_ids"]) | set(ids(r["evidence"])))
        else:
            agent["data_reach"].append({"system": r["system"], "access": r["access"], "via": ["inferred by LLM"],
                                        "rationale": r["rationale"], "evidence_ids": ids(r["evidence"]),
                                        "source": "llm"})
    have = {(t["type"], t.get("cron")) for t in agent["triggers"]}
    for t in card["triggers"]:
        if t["type"] in ("schedule", "event") and (t["type"], t["cron"] or None) not in have:
            agent["triggers"].append({"type": t["type"], "cron": t["cron"] or None, "description": t["description"],
                                      "evidence_ids": ids(t["evidence"]), "source": "llm"})
    agent["status"] = "needs_review" if agent["confidence"] < REVIEW_THRESHOLD else "enriched"
    agent["enrichment"] = card.get("_meta", {})
    return agent
