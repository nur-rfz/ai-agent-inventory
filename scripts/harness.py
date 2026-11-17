"""Shared test harness for the live demo path (acceptance checks + rehearsal).

Starts backend + demo MCP server + sensor in a throwaway HOME (Cursor already has a
GitHub server), seeds the simulated org, waits until background cards and cluster
confirmations are done (like the pre-warmed demo), then lets you add `inbox-digest`
to Cursor and watch what happens.
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
FAKE_GH = "Bearer " + "gh" + "p_" + "x" * 36


def port_open(p: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", p)) == 0


def wait_for(fn, timeout: float, interval: float = 0.05):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if (r := fn()) is not None:
                return r
        except httpx.HTTPError:
            pass
        time.sleep(interval)
    return None


class Stack:
    def __init__(self, port: int = 8796, db: Path | None = None, home: Path | None = None, sensor: bool = True,
                 replay_speed: float | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="sad-stack-"))
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.home = home or self.tmp / "home"
        self.cfg_path = self.home / ".cursor" / "mcp.json"
        self.db = db or self.tmp / "inv.db"
        self.with_sensor = sensor
        self.replay_speed = replay_speed
        self.env = {**os.environ, "PYTHONPATH": str(ROOT), "INVENTORY_DB": str(self.db), "BACKEND_URL": self.base,
                    "DEMO_OWNER_NAME": os.environ.get("DEMO_OWNER_NAME", "Presenter (you)")}
        self.procs: list[subprocess.Popen] = []
        self.log = open(self.tmp / "run.log", "w")
        self.api = httpx.Client(base_url=self.base, timeout=10)

    # --- lifecycle ---------------------------------------------------------------------
    def start(self) -> "Stack":
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg = {"mcpServers": {"github": {"url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": FAKE_GH}}}}
        self.cfg_path.write_text(json.dumps(self.cfg))
        self._spawn([PY, "-m", "uvicorn", "backend.app:app", "--port", str(self.port), "--log-level", "warning",
                     "--timeout-graceful-shutdown", "1"])
        if not port_open(8931):
            self._spawn([PY, "scripts/demo_mcp_server.py"])
        wait_for(lambda: (port_open(self.port) and port_open(8931)) or None, 30)
        return self

    def seed(self) -> None:
        self.api.post("/api/admin/reset").raise_for_status()
        cmd = [PY, "scripts/seed.py", "--backend", self.base]
        if self.replay_speed:
            cmd += ["--replay", "--speed", str(self.replay_speed)]
        subprocess.run(cmd, cwd=ROOT, env=self.env, check=True, stdout=self.log, stderr=self.log)

    def start_sensor(self) -> None:
        self._spawn([PY, "-m", "sensor.main"], env={**self.env, "HOME": str(self.home)})
        wait_for(lambda: self.agent_with("github") or None, 30)

    def stop(self) -> None:
        for p in reversed(self.procs):
            p.terminate()
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.log.close()

    def _spawn(self, cmd: list[str], env: dict | None = None) -> None:
        self.procs.append(subprocess.Popen(cmd, cwd=ROOT, env=env or self.env, stdout=self.log, stderr=self.log))

    # --- queries -----------------------------------------------------------------------
    def stats(self) -> dict:
        return self.api.get("/api/stats").json()

    def agents(self) -> list[dict]:
        return self.api.get("/api/agents").json()

    def clusters(self) -> list[dict]:
        return self.api.get("/api/clusters").json()

    def agent_with(self, member: str, pred=lambda a: True) -> dict | None:
        return next((a for a in self.agents() if any(m["name"] == member for m in a["members"]) and pred(a)), None)

    def settled(self) -> bool:
        st = self.stats()
        if not st["enrichment"]["enabled"]:
            return st["clustering"]["embedder_ready"] or bool(st["clustering"]["embedder_error"])
        cards_done = all(a["status"] in ("enriched", "needs_review") for a in self.agents())
        cl = self.clusters()
        return cards_done and st["clustering"]["embedder_ready"] and st["clustering"]["inflight"] == 0 and \
            all(c["status"] == "confirmed" for c in cl) and st["enrichment"]["inflight"] == 0

    def wait_settled(self, timeout: float = 240) -> bool:
        return bool(wait_for(lambda: self.settled() or None, timeout, 0.5))

    # --- the on-camera moment ------------------------------------------------------------
    def add_live_server(self) -> float:
        self.cfg["mcpServers"]["inbox-digest"] = {"url": "http://127.0.0.1:8931/mcp",
                                                  "headers": {"Authorization": "Bearer demo"}}
        t0 = time.time()
        self.cfg_path.write_text(json.dumps(self.cfg, indent=2))
        return t0

    def remove_live_server(self) -> None:
        self.cfg["mcpServers"].pop("inbox-digest", None)
        self.cfg_path.write_text(json.dumps(self.cfg, indent=2))
