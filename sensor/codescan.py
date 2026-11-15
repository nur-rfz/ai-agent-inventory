"""Find AI SDK imports and LLM-related env var NAMES in source code.

Values are never read out: for .env files we keep only the part before `=`.

CLI:  python -m sensor.codescan <folder> [--post]
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from common.schema import fingerprint

SDK_PATTERNS: dict[str, re.Pattern[str]] = {
    "openai": re.compile(r"^\s*(from\s+openai\b|import\s+openai\b)|require\(['\"]openai['\"]\)|from\s+['\"]openai['\"]", re.M),
    "anthropic": re.compile(r"^\s*(from\s+anthropic\b|import\s+anthropic\b)|['\"]@anthropic-ai/sdk['\"]", re.M),
    "claude_agent_sdk": re.compile(r"claude_agent_sdk|['\"]@anthropic-ai/claude-agent-sdk['\"]", re.M),
    "langchain": re.compile(r"^\s*(from|import)\s+langchain\w*|['\"]@?langchain[/\w\-]*['\"]", re.M),
    "langgraph": re.compile(r"^\s*(from|import)\s+langgraph|['\"]@langchain/langgraph['\"]", re.M),
    "llama_index": re.compile(r"^\s*(from|import)\s+llama_index", re.M),
    "google_genai": re.compile(r"google\.generativeai|from\s+google\s+import\s+genai|['\"]@google/gen(erative-)?ai['\"]", re.M),
    "mistral": re.compile(r"^\s*(from|import)\s+mistralai|['\"]@mistralai/mistralai['\"]", re.M),
    "cohere": re.compile(r"^\s*(from|import)\s+cohere\b|['\"]cohere-ai['\"]", re.M),
    "vercel_ai": re.compile(r"from\s+['\"]ai['\"]|['\"]@ai-sdk/[\w\-]+['\"]", re.M),
    "mcp_sdk": re.compile(r"^\s*(from|import)\s+mcp\b|['\"]@modelcontextprotocol/sdk[/\w.\-]*['\"]|fastmcp", re.M),
    "crewai": re.compile(r"^\s*(from|import)\s+crewai", re.M),
    "autogen": re.compile(r"^\s*(from|import)\s+autogen", re.M),
}
# Business-system SDKs: hint at data reach.
SYSTEM_SDKS: dict[str, re.Pattern[str]] = {
    "Microsoft 365": re.compile(r"msgraph|msal|graph\.microsoft\.com|@microsoft/microsoft-graph-client", re.M),
    "Gmail": re.compile(r"gmail|googleapiclient", re.M | re.I),
    "Salesforce": re.compile(r"simple_salesforce|jsforce|salesforce", re.M | re.I),
    "Slack": re.compile(r"slack_sdk|@slack/(web-api|bolt)|slack_bolt", re.M),
    "Jira": re.compile(r"^\s*(from|import)\s+jira\b|atlassian", re.M | re.I),
    "Postgres": re.compile(r"psycopg|asyncpg|sqlalchemy|['\"]pg['\"]", re.M),
    "GitHub": re.compile(r"PyGithub|from\s+github\s+import|@octokit", re.M),
    "HubSpot": re.compile(r"hubspot", re.M | re.I),
    "Zendesk": re.compile(r"zenpy|zendesk", re.M | re.I),
}
ENV_NAME = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:API_KEY|_TOKEN|_SECRET|_KEY|_ENDPOINT|_BASE_URL|_MODEL|_DEPLOYMENT)|"
    r"OPENAI_[A-Z_]+|ANTHROPIC_[A-Z_]+|AZURE_OPENAI_[A-Z_]+|LANGCHAIN_[A-Z_]+)\b"
)
# Visible prompts: string literals bound to *prompt* / *instructions* / the system role.
_PROMPT_LEAD = (
    r"(?:\b\w*(?:prompt|instructions)\w*\s*[:=]\s*"
    r"|[\"']role[\"']\s*:\s*[\"']system[\"']\s*,\s*[\"']content[\"']\s*:\s*"
    r"|\bsystem\s*=\s*)"
)
_STR = r"[frb]?(\"\"\"|'''|\"|')(.+?)\1"
PROMPT_LITERAL = re.compile(r"(?is)" + _PROMPT_LEAD + _STR)
DOCSTRING = re.compile(r"^\s*(?:#[^\n]*\n\s*)*[rb]?(\"\"\"|''')(.+?)\1", re.S)
MAX_PROMPT_CHARS = 600

CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".ipynb"}
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next", "site-packages"}
MAX_BYTES = 1_000_000


def extract_prompts(text: str, limit: int = 3) -> list[str]:
    out: list[str] = []
    for m in PROMPT_LITERAL.finditer(text):
        body = " ".join(m.group(2).split())
        if len(body) >= 20 and body not in out:
            out.append(body[:MAX_PROMPT_CHARS])
        if len(out) >= limit:
            break
    return out


def scan_text(text: str) -> dict:
    sdks = sorted(k for k, p in SDK_PATTERNS.items() if p.search(text))
    systems = sorted(k for k, p in SYSTEM_SDKS.items() if p.search(text)) if sdks else []
    env_names = sorted(set(ENV_NAME.findall(text)))
    res = {"sdks": sdks, "systems": systems, "env_names": env_names}
    if sdks:
        doc = DOCSTRING.match(text)
        res["docstring"] = " ".join(doc.group(2).split())[:MAX_PROMPT_CHARS] if doc else None
        res["prompts"] = extract_prompts(text)  # business text; scrubbed for secrets before emit
    return res


def scan_env_file(path: Path) -> list[str]:
    names = []
    try:
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name = line.split("=", 1)[0].removeprefix("export ").strip()
                if ENV_NAME.fullmatch(name):
                    names.append(name)  # value intentionally discarded
    except OSError:
        pass
    return names


def scan_file(path: Path) -> dict | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    res = scan_text(text)
    return res if res["sdks"] else None


def scan_folder(root: Path) -> list[dict]:
    results = []
    root = root.expanduser().resolve()
    env_names: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if fn.startswith(".env"):
                if n := scan_env_file(p):
                    env_names[str(p.relative_to(root))] = n
                continue
            if p.suffix in CODE_EXT and (res := scan_file(p)):
                results.append({"path": str(p), "relative_path": str(p.relative_to(root)), "root": str(root), **res})
    for rel, names in env_names.items():
        results.append({"path": str(root / rel), "relative_path": rel, "root": str(root),
                        "sdks": [], "systems": [], "env_names": names, "env_file": True})
    return results


def main() -> None:
    from .config import SensorConfig
    from .emitter import Emitter

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder")
    ap.add_argument("--post", action="store_true", help="send results to the backend")
    a = ap.parse_args()
    results = scan_folder(Path(a.folder))
    em = Emitter(SensorConfig.load().backend_url) if a.post else None
    for r in results:
        print(f"{r['relative_path']}: sdks={r['sdks']} systems={r['systems']} env={r['env_names']}")
        if em:
            em.emit("code_import", "sensor:codescan", r, fingerprint("script", r["path"]))
    if em:
        em.stop()


if __name__ == "__main__":
    main()
