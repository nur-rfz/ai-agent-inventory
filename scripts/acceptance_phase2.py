"""Phase 2 acceptance: replay the simulated org into a fresh backend and verify.

  python scripts/acceptance_phase2.py [--speed 1]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 8790


def port_open(p: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", p)) == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=1.0)
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="sad-p2-"))
    env = {**os.environ, "PYTHONPATH": str(ROOT), "INVENTORY_DB": str(tmp / "inv.db")}
    base = f"http://127.0.0.1:{PORT}"
    backend = subprocess.Popen([PY, "-m", "uvicorn", "backend.app:app", "--port", str(PORT), "--log-level", "warning", "--timeout-graceful-shutdown", "1"],
                               cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results: list[tuple[str, bool, str]] = []
    try:
        for _ in range(100):
            if port_open(PORT):
                break
            time.sleep(0.1)

        arrivals: list[tuple[float, dict]] = []
        t0 = time.monotonic()

        def listen():
            with httpx.stream("GET", f"{base}/api/stream", timeout=None) as r:
                ev = None
                for line in r.iter_lines():
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:") and ev == "signal":
                        arrivals.append((time.monotonic() - t0, json.loads(line[5:])))

        threading.Thread(target=listen, daemon=True).start()
        time.sleep(0.5)
        t0 = time.monotonic()
        subprocess.run([PY, "scripts/seed.py", "--backend", base, "--replay", "--speed", str(a.speed)],
                       cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
        time.sleep(0.5)

        sigs = [s for _, s in arrivals]
        span = arrivals[-1][0] - arrivals[0][0] if arrivals else 0
        print("\nLive arrival over SSE (signals per 5s window):")
        buckets = Counter(int(t // 5) * 5 for t, _ in arrivals)
        for b in sorted(buckets):
            hosts = sorted({s["host"] for t, s in arrivals if int(t // 5) * 5 == b})
            print(f"  {b:>3}-{b + 5:<3}s {'#' * (buckets[b] // 4):<40} {buckets[b]:>3}  {', '.join(hosts)}")
        results.append(("replay streams live over SSE (spread over time, not one burst)",
                        len(buckets) >= 4 and span > 15 / a.speed, f"{len(sigs)} signals over {span:.1f}s"))

        directory = yaml.safe_load((ROOT / "fixtures" / "directory.yaml").read_text())
        host_to = {d["host"]: p for p in directory["people"] for d in p["devices"]}
        email_to = {e: p for p in directory["people"] for e in [p["email"], *p.get("aliases", [])]}
        print("\nWhat each colleague contributes:")
        per = defaultdict(Counter)
        for s in sigs:
            person = host_to.get(s["host"]) or email_to.get(s["user"])
            per[person["name"] if person else s["host"]][s["kind"]] += 1
        for name, c in per.items():
            print(f"  {name:<16} {dict(c)}")

        print("\nAgents in the scenario (by evidence):")
        for s in sigs:
            r = s["raw"]
            if s["kind"] == "mcp_config":
                print(f"  {host_to[s['host']]['name']:<14} {r['client']:<15} MCP  {r['server_name']:<14} {r.get('url') or ' '.join(r['args'])}")
            elif s["kind"] == "schedule":
                print(f"  {host_to[s['host']]['name']:<14} {'cron':<15} job  {r['name']:<14} '{r['cron']}'")
            elif s["kind"] in ("oauth_grant", "api_key") and r.get("event") in ("consent", "app_role_assignment", "api_key.created"):
                who = email_to.get(r["user_email"], {}).get("name", r["user_email"])
                what = r.get("app_name") or r.get("project_name")
                print(f"  {who:<14} {r['provider']:<15} {r['event']:<16} {what}  {r.get('scopes') or r.get('owner_type')}")

        newest = max(datetime.fromisoformat(s["ts"]) for s in sigs)
        now = datetime.now(timezone.utc)
        results.append(("timestamps shifted to the last week (weekday kept)", newest <= now and (now - newest).days < 7,
                        f"newest event {newest.isoformat(timespec='minutes')} ({(now - newest).days}d ago)"))
        kinds = Counter(s["kind"] for s in sigs)
        results.append(("all signal kinds present",
                        {"mcp_config", "mcp_probe", "process", "net_conn", "code_import", "oauth_grant", "api_key", "schedule"} <= set(kinds),
                        str(dict(kinds))))

        again = subprocess.run([PY, "scripts/seed.py", "--backend", base], cwd=ROOT, env=env, capture_output=True, text=True)
        total = len(httpx.get(f"{base}/api/signals", params={"limit": 10000}).json())
        results.append(("re-seeding is idempotent", total == len(sigs), f"{total} stored after a second seed"))

        db = (tmp / "inv.db").read_bytes()
        bad = [x for x in ("fake-hubspot-private-app-token", "fake-slack-bot-token", "fake-github-pat", "fake-password")
               if x.encode() in db]
        results.append(("no fixture secrets in SQLite", not bad, "clean" if not bad else str(bad)))
    finally:
        backend.terminate()
        try:
            backend.wait(5)
        except subprocess.TimeoutExpired:
            backend.kill()

    print("\nPhase 2 acceptance")
    ok = True
    for name, passed, detail in results:
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
