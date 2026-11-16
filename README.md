# ai-agent-inventory: Shadow Agent Discovery → Langdock

Finds AI agents and MCP servers with no connectors to configure. It works out
what each one is for, groups duplicates built by different people, and turns
each group into one shared Langdock agent. See [ARCHITECTURE.md](ARCHITECTURE.md).

Status: **Phases 1–3 done** (sensor, simulated org, agent resolution + LLM enrichment). Phases 4–7 are next.

## Setup
```bash
python3 -m venv .venv && . .venv/bin/activate   # or: uv venv && uv pip install -r requirements.txt
pip install -r requirements.txt
```

## Run (Phase 1)
```bash
python -m uvicorn backend.app:app --port 8787        # terminal 1: backend
python scripts/demo_mcp_server.py                    # terminal 2: the MCP server you'll add on camera
python -m sensor.main                                # terminal 3: sensor (prints which config files it found)
```
Then add this to `~/.cursor/mcp.json` (or via Cursor → Settings → MCP):
```json
{"mcpServers": {"inbox-digest": {"url": "http://127.0.0.1:8931/mcp",
                                 "headers": {"Authorization": "Bearer anything"}}}}
```
Watch the signals arrive: `curl -N localhost:8787/api/stream`, or
`curl localhost:8787/api/signals`.

## Simulated colleagues (Phase 2)
```bash
python scripts/seed.py --reset --replay      # stream 6 colleagues + identity logs in ~30s
python scripts/seed.py                       # or load everything at once
```
Who built what: [fixtures/README.md](fixtures/README.md).

## Agents + LLM enrichment (Phase 3)
```bash
export OPENROUTER_API_KEY=...               # without it, cards stay rule-based
export DEMO_OWNER_NAME="Your Name"           # how your laptop shows up as owner
python -m uvicorn backend.app:app --port 8787
curl localhost:8787/api/agents               # inventory; /api/agents/<id> for evidence + graph
```
Optional: `LLM_MODEL` (default `openai/gpt-4o-mini`), `LLM_PROVIDER=anthropic` to use Claude instead.

## Checks
```bash
python -m pytest -q                       # redaction + parser tests
python scripts/acceptance_phase1.py       # end-to-end, in a throwaway HOME
python scripts/acceptance_phase2.py       # replay the org, verify live arrival + coverage
python scripts/acceptance_phase3.py       # live Cursor edit -> split agent + card within 5s
python -m sensor.paths                    # which MCP config files exist on this OS
```

stdio MCP servers are **not** spawned for probing unless you set `probe_stdio: true`
in `sensor.yaml` (see `sensor.example.yaml`).
