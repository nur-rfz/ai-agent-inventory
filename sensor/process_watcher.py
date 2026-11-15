"""Poll processes: spot MCP servers and scripts using AI SDKs, with their parent
chain (e.g. spawned by Cursor) and the systems they talk to.

We read cmdlines (redacted before emit) but never process environments.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable

import psutil

from common.redact import redact_args
from common.schema import fingerprint

from .codescan import scan_file
from .emitter import Emitter
from .netattr import NetAttributor, remote_endpoints
from .parsers import RawServer

log = logging.getLogger("sensor.proc")

RUNTIMES = {"npx", "uvx", "node", "python", "python3", "bun", "bunx", "deno", "docker", "uv", "pipx", "npm", "pnpm"}


def _base(p: str) -> str:
    b = os.path.basename(p or "").lower()
    for suf in (".exe", ".cmd"):
        b = b.removesuffix(suf)
    if b.startswith("python3."):
        b = "python3"
    return b


def parent_chain(proc: psutil.Process, depth: int = 8) -> list[dict]:
    chain = []
    try:
        p = proc.parent()
        while p and len(chain) < depth and p.pid > 1:
            try:
                chain.append({"pid": p.pid, "name": p.name(), "exe": _safe(p.exe)})
            except (psutil.AccessDenied, psutil.ZombieProcess):
                chain.append({"pid": p.pid, "name": "?", "exe": None})
            p = p.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return chain


def _safe(fn):
    try:
        return fn()
    except (psutil.AccessDenied, psutil.ZombieProcess, psutil.NoSuchProcess, OSError):
        return None


def surface_from_chain(chain: list[dict]) -> str | None:
    """Which AI client spawned this process (nearest matching ancestor wins)."""
    for hop in chain:
        name = (hop.get("name") or "").lower()
        exe = (hop.get("exe") or "").lower()
        hay = f"{name} {exe}"
        if "cursor" in hay:
            return "cursor"
        if "windsurf" in hay:
            return "windsurf"
        if "claude.app" in exe or name.startswith("claude helper") or "claude desktop" in hay:
            return "claude_desktop"
        if name == "claude" or "/claude-code" in exe or exe.endswith("/claude"):
            return "claude_code"
        if "visual studio code" in exe or name.startswith("code helper") or name in ("code", "code-insiders"):
            return "vscode"
    return None


def match_server(cmdline: list[str], servers: list[RawServer]) -> RawServer | None:
    """Does this process look like one of the configured stdio servers?"""
    joined = " ".join(cmdline)
    best = None
    for s in servers:
        if s.transport != "stdio" or not s.command:
            continue
        # npx/uvx resolve to node/python, so match on the distinctive args (package, script)
        distinctive = [a for a in s.args if not a.startswith("-") and len(a) > 2 and "=" not in a]
        if distinctive and all(a in joined for a in distinctive[:2]):
            best = s
            break
        if not distinctive and _base(s.command) == _base(cmdline[0]):
            best = s
    return best


class ProcessWatcher:
    def __init__(self, emitter: Emitter, net: NetAttributor, servers: Callable[[], list[RawServer]], poll_s: float = 2.0):
        self.emitter = emitter
        self.net = net
        self.servers = servers
        self.poll_s = poll_s
        self.tracked: dict[tuple[int, float], dict] = {}   # (pid, create_time) -> info
        self.seen: set[tuple[int, float]] = set()
        self._script_cache: dict[str, dict | None] = {}
        self._stop = threading.Event()
        self.me = os.getpid()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="proc", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                self.tick(initial=first)
            except Exception:  # never let one bad poll kill the watcher
                log.exception("process poll failed")
            first = False
            self._stop.wait(self.poll_s)

    def tick(self, initial: bool = False) -> None:
        self.net.maybe_refresh()
        alive: set[tuple[int, float]] = set()
        servers = self.servers()
        for s in servers:
            self.net.add_hosts(s.hostnames())
        for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time", "username"]):
            info = proc.info
            key = (info["pid"], info["create_time"] or 0.0)
            alive.add(key)
            if key in self.seen or info["pid"] == self.me:
                continue
            self.seen.add(key)
            hit = self._classify(proc, info, servers)
            if hit:
                hit["initial"] = initial
                self.tracked[key] = hit
                self.emitter.emit("process", "sensor:process", dict(hit), hit["fingerprint"])
        # exits
        for key in [k for k in self.tracked if k not in alive]:
            info = self.tracked.pop(key)
            pub = {k: v for k, v in info.items() if not k.startswith("_")}
            self.emitter.emit("process", "sensor:process",
                              {**pub, "event": "exited", "initial": False}, info["fingerprint"])
        self.seen &= alive
        # network attribution for tracked processes
        for key, info in self.tracked.items():
            eps, method = remote_endpoints(key[0])
            known = info.setdefault("_endpoints", set())
            for ip, port in eps:
                if (ip, port) in known:
                    continue
                attr = self.net.attribute(ip, port)
                if attr["hostname"] is None and attr["system"] is None and not attr["private"]:
                    continue  # retry next tick once reverse DNS has (maybe) answered
                known.add((ip, port))
                self.emitter.emit("net_conn", "sensor:net", {
                    "pid": key[0], "name": info["name"], "server_name": info.get("server_name"),
                    "method": method, **attr, "server_fingerprint": info.get("server_fingerprint"),
                }, info["fingerprint"])

    def _classify(self, proc: psutil.Process, info: dict, servers: list[RawServer]) -> dict | None:
        cmd = info.get("cmdline") or []
        if not cmd:
            return None
        exe = _base(cmd[0])
        if exe not in RUNTIMES and not any("mcp" in c.lower() for c in cmd[:3]):
            return None
        matched = match_server(cmd, servers)
        looks_mcp = matched is not None or any("mcp" in c.lower() for c in cmd)
        script = self._script_path(cmd, proc)
        sdk = self._scan_script(script) if script and not looks_mcp else None
        if not looks_mcp and not (sdk and sdk["sdks"]):
            return None
        chain = parent_chain(proc)
        fp = matched.fingerprint if matched else fingerprint("process", exe, redact_args(cmd[1:]))
        return {
            "event": "started",
            "pid": info["pid"],
            "name": info["name"],
            "exe": exe,
            "cmdline": redact_args(cmd),
            "started_at": info.get("create_time"),
            "username": info.get("username"),
            "parent_chain": chain,
            "surface": surface_from_chain(chain) or (matched.client if matched else None),
            "category": "mcp_server" if looks_mcp else "ai_script",
            "server_name": matched.name if matched else None,
            "server_fingerprint": matched.fingerprint if matched else None,
            "config_client": matched.client if matched else None,
            "script": script,
            "sdk_imports": sdk,
            "fingerprint": fp,
        }

    def _script_path(self, cmd: list[str], proc: psutil.Process) -> str | None:
        cwd = _safe(proc.cwd)
        for a in cmd[1:4]:
            if a.startswith("-"):
                continue
            p = Path(a)
            if p.suffix in (".py", ".js", ".mjs", ".ts", ".cjs"):
                if not p.is_absolute() and cwd:
                    p = Path(cwd) / p
                return str(p)
        return None

    def _scan_script(self, path: str) -> dict | None:
        if path not in self._script_cache:
            self._script_cache[path] = scan_file(Path(path))
        return self._script_cache[path]
