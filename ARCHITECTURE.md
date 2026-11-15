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
