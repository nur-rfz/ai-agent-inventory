"""Phase 3 acceptance: the live Cursor MCP gets a card within 5s of the config edit.

Starts backend + demo MCP server + sensor (throwaway HOME whose Cursor config already
has a GitHub server), seeds the simulated org, then adds `inbox-digest` to Cursor and
measures:
  - rule-based card (owner, tools, reach, classification, risk) visible
  - LLM card (name, purpose, classification, confidence) visible
  - every evidence id on the card resolves to a stored signal
  - the business server is a separate agent from the GitHub coding agent

  python scripts/acceptance_phase3.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8792


def port_open(p: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", p)) == 0


def wait_for(fn, timeout: float, interval: float = 0.05):
    end = time.time() + timeout
    while time.time() < end:
        if (r := fn()) is not None:
            return r
        time.sleep(interval)
    return None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sad-p3-"))
    home = tmp / "home"
    (home / ".cursor").mkdir(parents=True)
    cfg_path = home / ".cursor" / "mcp.json"
    cfg = {"mcpServers": {"github": {"url": "https://api.githubcopilot.com/mcp/",
                                     "headers": {"Authorization": "Bearer " + "gh" + "p_" + "x" * 36}}}}
    cfg_path.write_text(json.dumps(cfg))
    base = f"http://127.0.0.1:{PORT}"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "INVENTORY_DB": str(tmp / "inv.db"), "BACKEND_URL": base,
           "DEMO_OWNER_NAME": os.environ.get("DEMO_OWNER_NAME", "Presenter (you)")}
    log = open(tmp / "run.log", "w")
    procs = [subprocess.Popen([PY, "-m", "uvicorn", "backend.app:app", "--port", str(PORT), "--log-level", "warning",
                               "--timeout-graceful-shutdown", "1"], cwd=ROOT, env=env, stdout=log, stderr=log)]
    if not port_open(8931):
        procs.append(subprocess.Popen([PY, "scripts/demo_mcp_server.py"], cwd=ROOT, env=env, stdout=log, stderr=log))
    results: list[tuple[str, bool | None, str]] = []
    try:
        wait_for(lambda: (port_open(PORT) and port_open(8931)) or None, 15)
        subprocess.run([PY, "scripts/seed.py", "--backend", base], cwd=ROOT, env=env, check=True, stdout=log)
        procs.append(subprocess.Popen([PY, "-m", "sensor.main"], cwd=ROOT, env={**env, "HOME": str(home)},
                                      stdout=log, stderr=log))
        api = httpx.Client(base_url=base, timeout=5)
        wait_for(lambda: any(m["name"] == "github" for a in api.get("/api/agents").json() for m in a["members"]) or None, 15)
        enrich = api.get("/api/stats").json()["enrichment"]
        if enrich["enabled"]:  # like the pre-warmed demo: background cards done before going live
            wait_for(lambda: all(a["status"] in ("enriched", "needs_review") for a in api.get("/api/agents").json())
                     or None, 180, 0.5)
            enrich = api.get("/api/stats").json()["enrichment"]

        # --- the on-camera moment ---
        cfg["mcpServers"]["inbox-digest"] = {"url": "http://127.0.0.1:8931/mcp", "headers": {"Authorization": "Bearer demo"}}
        t0 = time.time()
        cfg_path.write_text(json.dumps(cfg, indent=2))

        def find(pred):
            for a in api.get("/api/agents").json():
                if any(m["name"] == "inbox-digest" for m in a["members"]) and pred(a):
                    return a
            return None

        first = wait_for(lambda: find(lambda a: True), 10)
        t_first = time.time() - t0
        rules = wait_for(lambda: find(lambda a: len(a["tools"]) >= 6), 10)
        t_rules = time.time() - t0
        results.append(("agent visible after config edit", bool(first), f"{t_first:.2f}s"))
        results.append(("owner, tools, reach, classification, risk visible (rules) < 5s", bool(rules) and t_rules < 5,
                        f"{t_rules:.2f}s"))

        card = None
        if enrich["enabled"]:
            card = wait_for(lambda: find(lambda a: a["status"] in ("enriched", "needs_review")), 30)
            t_card = time.time() - t0
            results.append(("LLM card (purpose, classification, confidence) < 5s", bool(card) and t_card < 5,
                            f"{t_card:.2f}s ({(card or {}).get('enrichment', {}).get('duration_ms')} ms model time, "
                            f"{enrich['provider']} {enrich['model']})"))
        else:
            results.append(("LLM card < 5s", None, f"SKIPPED: {enrich['disabled_reason']}"))

        a = card or rules or first
        agents = api.get("/api/agents").json()
        coding = [x for x in agents if any(m["name"] == "github" for m in x["members"]) and x["host"] == a["host"]]
        results.append(("split by purpose: separate from the GitHub coding agent in the same Cursor config",
                        bool(coding) and coding[0]["id"] != a["id"] and coding[0]["classification"] == "govern_in_place",
                        f"{a['name']} [{a['classification']}] vs {coding[0]['name'] if coding else '-'} "
                        f"[{coding[0]['classification'] if coding else '-'}]"))
        reach = {r["system"]: r["access"] for r in a["data_reach"]}
        results.append(("reach: Gmail rw, HubSpot r, Slack w", reach.get("Gmail") == "rw" and reach.get("HubSpot") == "r"
                        and reach.get("Slack") == "w", str(reach)))

        detail = api.get(f"/api/agents/{a['id']}").json()
        known = {s["id"] for s in detail["evidence"]}
        cited = {i for r in a["data_reach"] for i in r["evidence_ids"]} | \
                {i for f in a["risk"]["flags"] for i in f["evidence_ids"]} | \
                set(a.get("rationale", {}).get("purpose_evidence_ids") or [])
        results.append(("every cited evidence id resolves to a stored signal (clickable)", bool(cited) and cited <= known,
                        f"{len(cited)} cited, {len(cited - known)} unresolved"))

        print("\nCard for the live agent:")
        print(json.dumps({k: a.get(k) for k in ("name", "purpose", "owner", "surface", "classification",
                                                  "classification_source", "confidence", "status", "triggers",
                                                  "capabilities", "instructions_summary")}, indent=2, default=str)[:3000])
        print("  tools:", [t["name"] for t in a["tools"]])
        print("  reach:", [(r["system"], r["access"], r["source"]) for r in a["data_reach"]])
        print("  risk: ", a["risk"]["level"], [f["text"] for f in a["risk"]["flags"]])
        print("  graph:", [(e["from"].split(":")[0], e["to"].split(":")[0], e["label"]) for e in detail["graph"]["edges"]])
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        log.close()

    print("\nPhase 3 acceptance")
    ok = True
    for name, passed, detail in results:
        label = "SKIP" if passed is None else ("PASS" if passed else "FAIL")
        ok &= passed is not False
        print(f"  [{label}] {name}  {detail}")
    print(f"\nlogs: {tmp / 'run.log'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
