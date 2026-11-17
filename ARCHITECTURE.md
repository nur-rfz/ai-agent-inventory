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
signals ─▶ components ─▶ agents ─▶ reach · triggers · risk · rule class ─▶ LLM card (cached)
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
  govern_in_place, otherwise replace. The LLM may override with a rationale (not for `ok`).

### Enrichment (`backend/enrich.py`, `backend/llm.py`)
- Provider: **OpenRouter** by default (`openai/gpt-4o-mini`, OpenAI-compatible chat
  completions, `response_format: json_schema` strict). Anthropic is an option
  (`LLM_PROVIDER=anthropic`, `output_config.format`). Routing defaults to
  `provider.sort=throughput` on OpenRouter (~3s vs 3.6–5.3s per card at the time of
  testing; it currently routes to Azure-hosted OpenAI).
- Input: a compact, redacted evidence bundle. Each item has an id (E1…) that the card
  must cite; ids map back to signal ids, so the evidence on every card field is clickable.
  No timestamps or counts, so new runs of the same pattern don't change the hash.
- Output: a strict JSON schema. The LLM adds name, purpose, capabilities (Phase 4
  clustering), instructions summary (Phase 5), classification and confidence
  (< 0.6 → "needs review"; agents known only from an OAuth/key event are capped at 0.5).
  It can add reach seen in code or prompts, labeled as inferred, but never removes
  evidence-backed facts. Risk stays rule-based.
- Latency path for the live demo: agents first seen in the last 5 minutes go to a
  dedicated fast-lane worker, skip the 0.4s settle window once their probe is in, and
  pre-open the HTTPS connection on first sight. Everything else waits until its evidence
  has been unchanged for 0.4s, newest first.
- Cached in SQLite by (evidence hash, provider:model, prompt version); `reset` keeps the
  cache. Without credentials, cards stay rule-based (`rules_only`), with no fake output.
- Env: `OPENROUTER_API_KEY`, `LLM_PROVIDER` (openrouter | anthropic), `LLM_MODEL`,
  `OPENROUTER_PROVIDER_SORT` (default throughput; empty = OpenRouter default), and for
  Anthropic `ANTHROPIC_API_KEY`, `ENRICH_EFFORT`, `ENRICH_FALLBACKS`.

### API
`GET /api/agents`, `GET /api/agents/{id}` (evidence timeline + person→agent→server→system
graph + evidence bundle), `POST /api/agents/{id}/enrich`, `GET /api/stats`; SSE events
`agent`, `agent_removed`, `stats`, `signal`, `reset` on `/api/stream`.

## Duplicate clusters (Phase 4, `backend/cluster.py`)
```
enriched "replace" agents ─▶ score = 0.5·cosine(job+name+capabilities)        [all-MiniLM-L6-v2, local]
                                    + 0.25·same primary category of the job  [email / crm / documents …]
                                    + 0.25·Jaccard(system categories)         [Gmail ≈ Outlook = email]
                          ─▶ average-linkage groups (≥ 0.45)
                          ─▶ LLM confirmation: duplicate groups, base purpose, shared capabilities,
                             per-member variant ("uses Outlook instead of Gmail", "adds CRM lookup")
new agent near a confirmed group ─▶ LLM "join" check for the newcomer only (group label/variants stay put)
```
- Each card has a `job` field: 2–5 vendor-neutral words, so the same job built with
  Gmail, Outlook or a Python script reads the same.
- Why hybrid: on the fixtures, plain embeddings put real duplicates at 0.38–0.84 and
  unrelated pairs up to 0.48, so no single cosine cutoff works. The primary-category term
  keeps the core job decisive, so an inbox triager that also posts to Slack stays out
  of the Slack pipeline reports.
- Why average linkage: connected components chained the email and pipeline clusters
  through the live agent (it touches both). A regression test covers this.
- Live re-clustering is incremental: if a candidate group = a confirmed group +
  newcomers, the LLM only decides whether each newcomer belongs and what its variant is.
  The existing card updates in place (same id = its earliest member). It shows the
  newcomer as joining immediately, confirmed about 2s later.
- Confirmations are cached by membership + member evidence + model + prompt version.
  Without an LLM, groups show as unconfirmed `candidate`s.
- API: `GET /api/clusters`, `GET /api/clusters/{id}`; SSE event `clusters`.

## Langdock draft + export (Phase 5, `backend/langdock.py`)
What Langdock's public API supports (read on docs.langdock.com before building, Nov 2026):

| Need | Langdock | How we use it |
|---|---|---|
| Shared agent | `POST /agent/v1/create` (name ≤80, description ≤800, instruction ≤50k, emoji, inputType, conversationStarters, …), `POST /agent/v1/publish` | created + published after approval |
| Cron / recurring trigger | Scheduled Tasks `POST /automations/v1` (frequency DAILY/WEEKDAYS/WEEKLY/MONTHLY/SELECTED_WEEKDAYS, timeOfDay, timezone, assistantId) | one task per scheduled variant; cron mapped exactly. The Workflow API also exists, but its graph schema is the builder's own, so we don't generate workflows. |
| Gmail/Outlook/HubSpot/Salesforce/Slack/SharePoint | native integrations (Integration Directory) | mapped from the cluster's systems; enabling them and connecting OAuth is an admin step ("OAuth connections can't be pre-selected via the API") |
| MCP servers | UI only (URL, auth, "List Server Features"); private URLs blocked | admin step if a cluster needs a custom server |

Draft = deterministic mapping (integrations, schedules, decommission checklist, the exact
API requests) + LLM-written text (base instructions, one section per variant, starters,
knowledge suggestions), cached per cluster membership. Members' visible prompts are reused.

Flow: `GET /api/clusters/{id}/draft` → `POST …/approve` → `POST …/create`. Create sends the
requests only with `LANGDOCK_API_KEY` set; without it, it returns a dry run listing them.
Approval is tied to the draft's content hash, so a changed draft needs re-approval.
Exports: `GET …/export.json` (requests + manual steps), `GET …/export.md` (spec).

## UI (Phase 6, `ui/`)
React + Vite + Tailwind, served by the backend from `ui/dist` (one process for the demo;
`npm run dev` for hot reload). Live state comes from one `EventSource` on `/api/stream`
(`agent`, `agent_removed`, `clusters`, `stats`, `reset`).
- **Header:** agents found, unmanaged %, duplicate clusters, consolidation (N → M), high risk.
- **Inventory:** newest discoveries on top; new rows animate in. While the LLM works,
  rows show a shimmer plus the facts already known (tools, servers); the card fills in
  when it lands.
- **Agent drawer:** purpose with rationale, person → agent → server/script → system
  graph (write edges in red), reach, triggers, risk flags, tools, and an evidence
  timeline. Every "evidence" link highlights the exact signals; repeats collapse (×14);
  click a signal for its JSON.
- **Duplicates:** "N people built the same agent" cards with per-member variants. A
  newcomer appears as "just discovered" while the join is confirmed.
- **Consolidate:** the Langdock preview: N → 1, base + variants, rendered instructions
  (shimmer while being written), integrations, Scheduled Tasks, suggested knowledge,
  decommission checklist, the exact API requests, then Approve → Create in Langdock and
  JSON/Markdown export.

## Demo hardening (Phase 7)
- `scripts/demo.sh`: one command. Resets the DB, starts the demo MCP server, backend + UI
  and the sensor on your machine, replays the colleagues, prints the Cursor snippet.
- Reset keeps the LLM cache for the simulated org but **forgets everything about other
  agents**. Your live agent's evidence is the same every run, so without this the
  on-camera card would be a cache hit instead of a live LLM call.
- `scripts/prewarm.py`: replays the org at demo speed into `data/inventory.db` once, so
  every intermediate state of the replay is cached and only the live agent calls the LLM.
- Latency path for the live agent: a fast-lane worker pool (2) for agents first seen in
  the last 5 minutes, no settle wait once the probe is in, the HTTPS connection to the
  LLM pre-opened on first sight, OpenRouter throughput routing, and an incremental
  cluster join.
- Progressive card: deterministic facts render at about 1s; LLM fields fill in.
- `scripts/rehearse.py --runs 5`: the full on-camera path through the real UI with
  Playwright (row → card → cluster badge → confirmed → preview incl. live → approve →
  create), with timings per step. The last run: 5/5 clean, see README.
