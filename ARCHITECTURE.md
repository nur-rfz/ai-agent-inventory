# Architecture

```
 your laptop                                   backend (FastAPI + SQLite)               UI (React, Phase 6)
┌──────────────────────────────┐   POST      ┌────────────────────────────────┐  SSE  ┌──────────────┐
│ sensor                       │  /api/      │ ingest → scrub → Signal table  │──────▶│ live         │
│  mcp_watcher  (watchdog+poll)│  signals    │ (Phase 3) resolve → Agent      │       │ inventory    │
│  process_watcher (psutil 2s) │────────────▶│ (Phase 3) enrich (Claude)      │       └──────────────┘
│  netattr (psutil → lsof)     │             │ (Phase 4) cluster (embeddings) │
│  probe (mcp SDK, 5s timeout) │             │ (Phase 5) Langdock draft       │
│  codescan (on demand)        │             └────────────────────────────────┘
│  emitter: scrub → queue      │                       ▲
└──────────────────────────────┘                       │ fixtures/ (Phase 2: simulated colleagues,
                                                       │ Entra / Google Workspace OAuth logs)
```

## Signals (the only thing that crosses the wire)
`{id, ts, source, host, user, kind, raw, fingerprint}`. `kind` is one of
`mcp_config | process | net_conn | mcp_probe | code_import | schedule | oauth_grant | api_key`.
`fingerprint` says *what the signal is about*. For MCP servers it is
sha256(command + redacted args) or sha256(redacted url), so the config entry, the
running process and the probe result for the same server share one fingerprint.
That shared value is how Phase 3 joins them into one Agent. Scripts use
sha256("script", absolute path), shared by the code scan, the cron/launchd entry and
the running process.

## Sensor (Phase 1)
| Watcher | How | Emits |
|---|---|---|
| MCP configs | watchdog on each config's folder + 1s stat poll (catches folders created later and atomic-rename saves); 500ms debounce per file; diff against last parse | `mcp_config` per server `added/changed/removed` |
| Probe | HTTP/SSE servers: `initialize` + `tools/list` via official `mcp` SDK, 5s timeout. stdio: only if `probe_stdio: true` (consent), else a `skipped` signal | `mcp_probe` with tools + schemas |
| Processes | psutil every 2s; MCP-looking cmdlines (runtime + "mcp", or matches a configured server's args), or scripts whose source imports an AI SDK; parent chain → surface (Cursor, Claude Code, …) | `process started/exited` |
| Network | outbound connections of tracked processes; IP → system via forward-resolved catalog hosts, reverse DNS for wildcards, well-known ports; psutil → `lsof` → `unavailable` | `net_conn` |
| Code scan | `python -m sensor.codescan <dir> --post`: SDK imports, business SDKs, env var **names**, visible prompt literals + module docstring | `code_import` |
| Schedules | `crontab -l` + macOS `~/Library/LaunchAgents/*.plist` every 60s; script paths resolved (incl. `cd dir &&`) | `schedule` (the `triggers` evidence) |

Config locations are OS-specific (`sensor/paths.py`; run `python -m sensor.paths` to see
what was found). Claude Code's `~/.claude.json` also lists project folders, which are
added to the watch list automatically.

## Security: secrets never leave the sensor
1. Parsers keep real env/header values in memory only (needed to probe HTTP servers with auth).
2. `RawServer.public()` keeps env/header **names** and drops their values. It also redacts
   CLI args (`--token X`, `KEY=v`, URL passwords, sensitive query params, token shapes).
3. `scrub()` runs on every signal in the emitter and **again** on ingest in the backend.
4. Emitter guard: if a secret-shaped string survives scrubbing, the signal body is dropped.
5. Process environments are never read. Code scan reads `.env` names only.
6. Fingerprints are hashed from *redacted* data, so rotating a key doesn't fork an agent
   and a hash can't be brute-forced back to the key.

Tests: `tests/test_redact.py` (token shapes, URLs, args, parsed configs, structural ids
preserved). `scripts/acceptance_phase1.py` greps the API output and the raw SQLite
bytes for the secrets it planted.

## Simulated org (Phase 2)
`fixtures/` holds six colleagues' real artifacts (MCP configs, scripts, crontabs) plus
vendor-shaped identity exports. `fixtures/build.py` runs the **sensor's own code** over
those artifacts, so fixture signals are byte-for-byte what a live sensor would send.
`backend/identity.py` turns the exports into signals (Entra consents and app roles,
Google OAuth `authorize`, OpenAI key and service-account creation); the same code would
run on real exports. `scripts/seed.py --replay` streams everything in about 30s, with
timestamps shifted by whole weeks to end just before "now". See `fixtures/README.md`.

```
identity exports ──▶ backend/identity.py ─┐
fixtures/colleagues ─▶ fixtures/build.py ─┴─▶ scripts/seed.py ──POST /api/signals──▶ backend
(real sensor code)      (signals.jsonl)        (week shift, replay pacing)
```

## Agents (Phase 3)
```
signals ─▶ components ─▶ agents ─▶ reach · triggers · risk · rule class ─▶ Claude card (cached)
           (per fingerprint)  (split by purpose,      (deterministic,          (name, purpose, capabilities,
                               identity linked)        every fact cites ids)    classification, confidence)
```
- **Components** (`backend/resolve.py`): one per thing on one host, joined by the shared
  fingerprint. An MCP server in a config file plus its probe, processes and connections;
  a script plus its code scan, cron entry, runs and `.env`; an OAuth app or an LLM vendor
  project from the identity logs.
- **Split by purpose**: MCP servers in one client config are grouped by domain.
  Engineering systems (GitHub, Postgres, Sentry, Linear, …) form a coding agent and
  everything else a business agent. A Gmail/HubSpot/Slack server added to a Cursor config
  that already has GitHub becomes its own agent. Agent ids are stable when servers are
  added.
- **Owners** (`backend/org.py`): (host, local user) and emails/aliases → the directory. An
  unknown laptop (the presenter's) uses `DEMO_OWNER_NAME`.
- **Identity linking**: an OAuth app or LLM project links to the same owner's agent when
  the app name or the systems match (e.g. "ms-365-mcp-server" consent → Jonas's Outlook MCP;
  "AR Inbox Summarizer" app + OpenAI "Finance Automation" key → Priya's script). Unlinked
  ones become SaaS agents (ChatGPT, Fireflies, Langdock).
- **Reach**: tools/list (verb → r/w, description → system), known-package catalog
  (`common/mcp_packages.yaml`, for servers we can't probe), network connections, code SDKs
  and write calls (`chat_postMessage` → Slack w), OAuth scopes.
- **Triggers**: cron/launchd entries; otherwise the observed run pattern
  ("interactive in Claude Desktop, weekdays ~08:00").
- **Risk** (rules + `fixtures/policy.yaml`): write access to systems of record,
  personal/user OAuth on company data, tenant-wide application permissions, LLMs outside
  approved providers, service-account keys, plaintext credentials, unrestricted DB access.
  Every flag cites signal ids.
- **Classification** (rules first): sanctioned → ok, engineering → govern_in_place, SaaS →
  govern_in_place, otherwise replace. Claude may override with a rationale (not for `ok`).

### Enrichment (`backend/enrich.py`)
- Input: a compact, redacted evidence bundle. Each item has an id (E1…) that the card
  must cite; ids map back to signal ids, so the evidence on every card field is clickable.
  No timestamps or counts, so new runs of the same pattern don't change the hash.
- Output: `output_config.format` with a strict JSON schema (forced `tool_choice` is
  rejected by current models). Claude adds name, purpose, capabilities (used for Phase 4
  clustering), instructions summary (Phase 5), classification and confidence
  (< 0.6 → "needs review"). It can add reach it sees in code or prompts, labeled as
  inferred, but it never removes evidence-backed facts. Risk stays rule-based.
- Cached in SQLite by (evidence hash, model, prompt version); `reset` keeps the cache.
  The backend calls Claude only once an agent's evidence has been unchanged for 0.6s.
  Without credentials, cards stay rule-based (`rules_only`), with no fake output.
- Env: `ANTHROPIC_API_KEY`, `ENRICH_MODEL` (default `claude-opus-5-5`), `ENRICH_EFFORT`
  (default `low`, for demo latency), `ENRICH_FALLBACKS` (server-side refusal fallbacks,
  default on).

### API
`GET /api/agents`, `GET /api/agents/{id}` (evidence timeline + person→agent→server→system
graph + evidence bundle), `POST /api/agents/{id}/enrich`, `GET /api/stats`; SSE events
`agent`, `agent_removed`, `stats`, `signal`, `reset` on `/api/stream`.
