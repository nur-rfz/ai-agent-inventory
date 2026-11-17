"""Rehearse the 60-second live path end to end, N times, through the real UI.

Each run: reset (keeps pre-warmed fixture caches, forgets everything about the live
agent so it really calls the LLM), seed the simulated org, start the sensor, then a
headless browser edits ~/.cursor/mcp.json (throwaway HOME) and waits for:
row visible -> card filled -> "4 people built this" -> cluster confirmed ->
Langdock preview incl. the live agent -> approve -> create (dry run without a key).

    python scripts/rehearse.py [--runs 5] [--shots DIR]
Needs Node + Playwright (`require('playwright')`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from harness import ROOT, Stack, wait_for

STEPS = ["row_visible", "card_filled", "cluster_badge", "cluster_confirmed", "preview_open", "preview_includes_live",
         "approved", "create_result"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--shots", default=None, help="save screenshots of the first run here")
    a = ap.parse_args()

    s = Stack(port=8800).start()
    runs = []
    try:
        print("pre-warming fixture caches…", flush=True)
        s.seed()
        s.wait_settled(300)
        for i in range(a.runs):
            s.api.post("/api/admin/reset").raise_for_status()  # forget the live agent's cached LLM results
            s.remove_live_server()
            s.seed()
            sensor = subprocess.Popen([sys.executable, "-m", "sensor.main"], cwd=ROOT,
                                      env={**s.env, "HOME": str(s.home)}, stdout=s.log, stderr=s.log)
            try:
                wait_for(lambda: s.agent_with("github") or None, 30)
                s.wait_settled(240)
                out = subprocess.run(["node", str(ROOT / "scripts" / "rehearse_ui.js"), s.base, str(s.cfg_path)]
                                     + ([a.shots] if a.shots and i == 0 else []),
                                     capture_output=True, text=True, timeout=240)
                line = (out.stdout.strip().splitlines() or ["{}"])[-1]
                r = json.loads(line) if line.startswith("{") else {"ok": False, "error": out.stderr[-300:]}
            finally:
                sensor.terminate()
                sensor.wait(5)
                s.cfg = json.loads(s.cfg_path.read_text())
            runs.append(r)
            status = "OK " if r.get("ok") else "ERR"
            print(f"run {i + 1}: {status} " + "  ".join(f"{k}={r[k]}s" for k in STEPS if k in r)
                  + (f"  card='{r.get('card_name')}'" if r.get("card_name") else "")
                  + (f"  error={r.get('failed_at')}: {r.get('error')}" if not r.get("ok") else "")
                  + (f"  page_errors={r['errors']}" if r.get("errors") else ""), flush=True)
            time.sleep(1)
    finally:
        s.stop()

    ok = [r for r in runs if r.get("ok")]
    print(f"\nRehearsal: {len(ok)}/{len(runs)} runs without errors")
    if ok:
        print(f"{'step':<24}{'min':>7}{'median':>8}{'max':>7}   (seconds after the Cursor config edit)")
        for k in STEPS:
            vals = sorted(r[k] for r in ok if k in r)
            if vals:
                print(f"{k:<24}{vals[0]:>7.1f}{vals[len(vals) // 2]:>8.1f}{vals[-1]:>7.1f}")
    return 0 if len(ok) == len(runs) else 1


if __name__ == "__main__":
    sys.exit(main())
