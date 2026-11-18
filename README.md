# ai-agent-inventory: Shadow Agent Discovery → Langdock

Finds the AI agents and MCP servers people built on their own, with no connectors to
configure. It works out what each one is for from scattered signals, groups duplicates
built by different people, and turns each group into one shared Langdock agent (base +
variants). See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works.

**The demo moment:** add an MCP server in Cursor → about 1s later it's in the inventory →
about 5s later it has a purpose, owner, tools, reach and risk → it joins "Customer email
summarizer" (now **4 people built the same agent**) → one click previews a single
consolidated Langdock agent → Approve → Create.

Screenshots: [inventory](screenshots/1-inventory.png) ·
[agent detail](screenshots/2-agent-detail.png) · [duplicates](screenshots/3-duplicates.png) ·
[Langdock preview](screenshots/4-langdock-preview.png)

## Run the demo (one command)
```bash
export OPENROUTER_API_KEY=...          # LLM for cards/clusters/drafts (openai/gpt-4o-mini); without it: rule-based cards
export DEMO_OWNER_NAME="Your Name"     # how your laptop shows up as owner
scripts/demo.sh                        # sets up venv + UI on first run, then opens http://127.0.0.1:8787
```
`demo.sh` resets the database (keeps the pre-warmed LLM results for the simulated org),
starts the demo MCP server, the backend + UI and the sensor on **your** machine, and
replays six simulated colleagues + identity logs over about 30s. Options: `--fast`
(load colleagues instantly), `--no-seed` (only your machine). Ctrl-C stops everything.

On camera, add this in Cursor → Settings → MCP (or `~/.cursor/mcp.json`):
```json
"inbox-digest": { "url": "http://127.0.0.1:8931/mcp", "headers": { "Authorization": "Bearer demo" } }
```

### Before recording
```bash
python scripts/prewarm.py              # caches the colleagues' cards/clusters/drafts (only your agent calls the LLM live)
python scripts/rehearse.py --runs 5    # the live path end to end through the UI, 5x (needs Node + Playwright)
```
Last rehearsal (cloud container, gpt-4o-mini via OpenRouter), in seconds after the
Cursor config edit, as median (max):

| row visible | card filled | "4 people built this" | cluster confirmed | Langdock preview incl. you | approve → create |
|---|---|---|---|---|---|
| 0.8 (0.9) | 5.0 (6.9) | 5.1 (7.0) | 8.7 (9.4) | 17.7 (18.8) | 17.8 (18.9) |

## Configuration
| Env | Default | |
|---|---|---|
| `OPENROUTER_API_KEY` | – | LLM key (cards, cluster confirmation, Langdock instructions) |
| `LLM_MODEL` | `openai/gpt-4o-mini` | any OpenRouter model with JSON-schema output |
| `OPENROUTER_PROVIDER_SORT` | `throughput` | OpenRouter routing (currently Azure-hosted OpenAI); empty = OpenRouter default |
| `LLM_PROVIDER` | `openrouter` | or `anthropic` (+ `ANTHROPIC_API_KEY`) |
| `LANGDOCK_API_KEY` | – | if set, "Create in Langdock" sends the requests; otherwise a dry run lists them |
| `LANGDOCK_BASE_URL` | `https://api.langdock.com` | dedicated deployments: `<url>/api/public` |
| `LANGDOCK_TIMEZONE` | `Europe/Berlin` | timezone for Scheduled Tasks |
| `DEMO_OWNER_NAME` | local user | owner shown for this laptop |
| `sensor.yaml` | see `sensor.example.yaml` | stdio MCP probing is off unless you opt in (`probe_stdio: true`) |

## Development
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python -m uvicorn backend.app:app --port 8787    # API + built UI
(cd ui && npm install && npm run dev)            # UI with hot reload on :5173 (proxies /api)
python -m sensor.main                            # sensor
python scripts/seed.py --replay                  # simulated colleagues
```

## Checks
```bash
python -m pytest -q                        # redaction, parsers, resolution, clustering, Langdock mapping (53 tests)
python scripts/acceptance_phase1.py        # config edit -> redacted signal < 2s, probe returns tools
python scripts/acceptance_phase2.py        # simulated org streams live, no secrets stored
python scripts/acceptance_phase3.py        # live Cursor edit -> its own agent + card within ~5s
python scripts/acceptance_phase4.py        # duplicate clusters; live agent joins the email cluster
python scripts/acceptance_phase5.py        # Langdock draft incl. live agent, approve -> create
python scripts/rehearse.py --runs 5        # the whole on-camera path through the UI
python -m sensor.paths                     # which MCP config files exist on this OS
```

## What's real and what's simulated
- **Real:** the sensor on your machine (MCP configs for Cursor, Claude Desktop, VS Code,
  Windsurf and Claude Code; processes; network attribution; cron/LaunchAgents; code scan),
  the MCP probe, redaction, resolution, LLM enrichment, embeddings, clustering, and the
  Langdock request plan built from Langdock's documented API.
- **Simulated:** the six colleagues' laptops (`fixtures/`, produced by running the real
  sensor code over their config files and scripts) and the Entra / Google Workspace /
  OpenAI admin exports (vendor-shaped JSON).
- **Not built (non-goals):** org-wide endpoint deployment, MDM, proxy/DNS ingestion,
  auth/multi-tenancy, blocking.
