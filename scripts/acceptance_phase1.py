"""Phase 1 acceptance check.

Starts backend + sensor + the demo HTTP MCP server, then edits ~/.cursor/mcp.json
(in a throwaway HOME by default, so your real Cursor config is untouched) and checks:
  1. a redacted mcp_config signal reaches the backend in < 2s
  2. the probe returns the server's tools
  3. no secret written into the config appears anywhere in the API or the DB file
  4. edits produce `changed` / `removed` diffs

  python scripts/acceptance_phase1.py            # sandboxed HOME
  python scripts/acceptance_phase1.py --real-home  # your actual ~/.cursor/mcp.json (backed up + restored)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
# Fake secrets, assembled at runtime so GitHub push protection doesn't flag the source.
SECRETS = {
    "header": "Bearer " + "sk-" + "proj-DEMOsecret9f8e7d6c5b4a3928170",
    "gh": "gh" + "p_DEMOabcdefghijklmnopqrstuvwxyz012345",
    "pg": "Pg-Passw0rd-DEMO",
}


def port_open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for(fn, timeout: float, interval: float = 0.05):
    end = time.time() + timeout
    while time.time() < end:
        if (r := fn()) is not None:
            return r
        time.sleep(interval)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real-home", action="store_true")
    ap.add_argument("--backend-port", type=int, default=8788)
    a = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="sad-accept-"))
    home = Path.home() if a.real_home else tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    cursor_cfg = home / ".cursor" / "mcp.json"
    backup = None
    if a.real_home and cursor_cfg.exists():
        backup = tmp / "mcp.json.bak"
        shutil.copy2(cursor_cfg, backup)

    base = f"http://127.0.0.1:{a.backend_port}"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "INVENTORY_DB": str(tmp / "inv.db"), "BACKEND_URL": base}
    procs = []
    log = open(tmp / "run.log", "w")
    results: list[tuple[str, bool, str]] = []
    try:
        procs.append(subprocess.Popen([PY, "-m", "uvicorn", "backend.app:app", "--port", str(a.backend_port),
                                       "--log-level", "warning", "--timeout-graceful-shutdown", "1"], cwd=ROOT, env=env, stdout=log, stderr=log))
        if not port_open(8931):
            procs.append(subprocess.Popen([PY, "scripts/demo_mcp_server.py"], cwd=ROOT, env=env, stdout=log, stderr=log))
        wait_for(lambda: port_open(a.backend_port) and port_open(8931) or None, 15)
        sensor_env = {**env, "HOME": str(home)}
        procs.append(subprocess.Popen([PY, "-m", "sensor.main"], cwd=ROOT, env=sensor_env, stdout=log, stderr=log))
        time.sleep(2.5)  # let the sensor take its baseline

        api = httpx.Client(base_url=base, timeout=5)

        def find(kind: str, name: str, pred=lambda r: True):
            for s in api.get("/api/signals", params={"kind": kind, "limit": 500}).json():
                if s["raw"].get("server_name") == name and pred(s["raw"]):
                    return s
            return None

        # 1. add a server (the ~/.cursor dir may not even exist yet)
        cursor_cfg.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(cursor_cfg.read_text()) if cursor_cfg.exists() else {}
        servers = dict(existing.get("mcpServers", {}))
        servers["inbox-digest"] = {"url": "http://127.0.0.1:8931/mcp", "headers": {"Authorization": SECRETS["header"]}}
        t0 = time.time()
        cursor_cfg.write_text(json.dumps({**existing, "mcpServers": servers}, indent=2))
        sig = wait_for(lambda: find("mcp_config", "inbox-digest", lambda r: r.get("change") == "added"), 10)
        dt = time.time() - t0
        results.append(("config edit -> backend signal < 2s", bool(sig) and dt < 2, f"{dt:.2f}s"))

        # 2. probe returns tools
        pr = wait_for(lambda: find("mcp_probe", "inbox-digest", lambda r: r.get("status") == "ok"), 8)
        tools = [t["name"] for t in (pr or {}).get("raw", {}).get("tools", [])]
        results.append(("probe returns tools", bool(tools), f"{len(tools)} tools: {', '.join(tools)} ({time.time() - t0:.2f}s after edit)"))

        # 4. change + add a stdio server with secrets, then remove
        servers["github"] = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                             "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRETS["gh"]}}
        servers["crm-db"] = {"command": "uvx", "args": ["postgres-mcp", f"postgresql://app:{SECRETS['pg']}@db.internal:5432/crm"]}
        servers["inbox-digest"]["headers"]["X-Team"] = "support"
        cursor_cfg.write_text(json.dumps({**existing, "mcpServers": servers}, indent=2))
        ch = wait_for(lambda: find("mcp_config", "inbox-digest", lambda r: r.get("change") == "changed"), 5)
        gh = wait_for(lambda: find("mcp_config", "github", lambda r: r.get("change") == "added"), 5)
        skip = wait_for(lambda: find("mcp_probe", "github", lambda r: r.get("status") == "skipped"), 5)
        del servers["crm-db"]
        cursor_cfg.write_text(json.dumps({**existing, "mcpServers": servers}, indent=2))
        rm = wait_for(lambda: find("mcp_config", "crm-db", lambda r: r.get("change") == "removed"), 5)
        results.append(("diffs: changed / added / removed", bool(ch and gh and rm), ""))
        results.append(("stdio server NOT spawned without consent", bool(skip), (skip or {}).get("raw", {}).get("reason", "")))

        # 3. secrets never stored
        time.sleep(0.5)
        dump = json.dumps(api.get("/api/signals", params={"limit": 5000}).json())
        db_bytes = (tmp / "inv.db").read_bytes()
        needles = list(SECRETS.values()) + ["sk-" + "proj-DEMO", "gh" + "p_DEMO"]
        leaked = [n for n in needles if n in dump or n.encode() in db_bytes]
        results.append(("no secrets in API output or SQLite file", not leaked, f"leaked: {leaked}" if leaked else "clean"))

        if sig:
            print("\nSample stored signal (mcp_config):")
            print(json.dumps({k: sig[k] for k in ("kind", "source", "host", "user", "fingerprint")} |
                             {"raw": {k: sig["raw"][k] for k in ("server_name", "client", "transport", "url", "header_keys", "change")}}, indent=2))
        gh_raw = (gh or {}).get("raw", {})
        if gh_raw:
            print("\nSample stored signal (github, stdio): env =", gh_raw.get("env"))
        crm = find("mcp_config", "crm-db")
        if crm:
            print("crm-db args =", crm["raw"]["args"])
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        if a.real_home:
            if backup:
                shutil.copy2(backup, cursor_cfg)
            else:
                cursor_cfg.unlink(missing_ok=True)
        log.close()

    print("\nPhase 1 acceptance")
    ok = True
    for name, passed, detail in results:
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  {detail}")
    print(f"\nlogs: {tmp / 'run.log'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
