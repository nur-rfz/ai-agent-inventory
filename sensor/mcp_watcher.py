"""Watch MCP config files; emit one signal per server added / changed / removed.

watchdog gives us low-latency events; a 1s stat poll is the safety net for
directories that don't exist yet (e.g. ~/.cursor before Cursor is installed)
and for editors that save via atomic rename.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import SensorConfig
from .emitter import Emitter
from .parsers import RawServer, claude_code_projects, parse_file
from .paths import ConfigLocation, project_locations, user_locations
from .probe import probe, stdio_allowed

log = logging.getLogger("sensor.mcp")

ServerKey = tuple[str, str, str]  # (scope, project, name) within one file


def _key(s: RawServer) -> ServerKey:
    return (s.scope, s.project or "", s.name)


class McpConfigWatcher:
    def __init__(self, cfg: SensorConfig, emitter: Emitter, on_servers_changed: Callable[[], None] | None = None):
        self.cfg = cfg
        self.emitter = emitter
        self.on_servers_changed = on_servers_changed
        self.locations: dict[Path, ConfigLocation] = {}
        self.state: dict[Path, dict[ServerKey, RawServer]] = {}
        self._public: dict[Path, dict[ServerKey, dict]] = {}
        self._stat: dict[Path, tuple] = {}
        self._timers: dict[Path, threading.Timer] = {}
        self._lock = threading.RLock()
        self._observer = Observer()
        self._watched_dirs: set[Path] = set()
        self._probe_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe")
        self._stop = threading.Event()

        for loc in user_locations():
            self.locations[loc.path] = loc
        roots = list(cfg.project_roots)
        cc = next((l.path for l in self.locations.values() if l.fmt == "claude_code_user"), None)
        if cc:
            roots += claude_code_projects(cc)
        for r in dict.fromkeys(roots):
            self.add_project_root(Path(r).expanduser())

    # -- public API ---------------------------------------------------------
    def add_project_root(self, root: Path) -> None:
        for loc in project_locations(root):
            self.locations.setdefault(loc.path, loc)

    def all_servers(self) -> list[RawServer]:
        with self._lock:
            return [s for d in self.state.values() for s in d.values()]

    def start(self) -> None:
        for path in list(self.locations):
            self._process(path, initial=True)
        self._observer.start()
        self._refresh_watches()
        threading.Thread(target=self._poll_loop, name="mcp-poll", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._observer.stop()
        self._probe_pool.shutdown(wait=False, cancel_futures=True)

    # -- watching ---------------------------------------------------------------
    def _refresh_watches(self) -> None:
        handler = _Handler(self)
        for path in self.locations:
            d = path.parent
            if d.is_dir() and d not in self._watched_dirs:
                try:
                    self._observer.schedule(handler, str(d), recursive=False)
                    self._watched_dirs.add(d)
                except OSError as e:
                    log.debug("cannot watch %s: %s", d, e)

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.cfg.config_poll_s):
            self._refresh_watches()
            for path in list(self.locations):
                st = _stat(path)
                if st != self._stat.get(path):
                    self.schedule(path)

    def schedule(self, path: Path) -> None:
        """Debounce: (re)start a timer per file; parse when edits settle."""
        if path not in self.locations:
            return
        with self._lock:
            t = self._timers.get(path)
            if t:
                t.cancel()
            t = threading.Timer(self.cfg.debounce_s, self._process, args=(path,))
            t.daemon = True
            self._timers[path] = t
            t.start()

    # -- diffing ----------------------------------------------------------------
    def _process(self, path: Path, initial: bool = False) -> None:
        loc = self.locations[path]
        with self._lock:
            self._stat[path] = _stat(path)
            servers = parse_file(loc)
            if servers is None:  # half-written file: wait for next event
                return
            new = {_key(s): s for s in servers}
            new_pub = {k: s.public() for k, s in new.items()}
            old_pub = self._public.get(path, {})
            changes: list[tuple[str, RawServer | None, dict]] = []
            for k, pub in new_pub.items():
                if k not in old_pub:
                    changes.append(("added", new[k], pub))
                elif old_pub[k] != pub:
                    changes.append(("changed", new[k], pub))
            for k, pub in old_pub.items():
                if k not in new_pub:
                    changes.append(("removed", self.state[path][k], pub))
            self.state[path] = new
            self._public[path] = new_pub
            if loc.fmt == "claude_code_user":  # new Claude Code projects -> new folders to watch
                for p in claude_code_projects(path):
                    self.add_project_root(Path(p))

        for change, server, pub in changes:
            self.emitter.emit(
                "mcp_config",
                f"sensor:mcp_config:{loc.client}",
                {**pub, "change": change, "initial": initial, "server_fingerprint": server.fingerprint},
                server.fingerprint,
            )
            if change in ("added", "changed") and not server.disabled:
                self._maybe_probe(server)
        if changes and self.on_servers_changed:
            self.on_servers_changed()

    def _maybe_probe(self, server: RawServer) -> None:
        if server.transport in ("http", "sse") or stdio_allowed(
            server, self.cfg.probe_stdio, self.cfg.probe_stdio_allowlist
        ):
            self._probe_pool.submit(self._run_probe, server)
        elif server.transport == "stdio":
            self.emitter.emit(
                "mcp_probe",
                "sensor:mcp_probe",
                {"server_name": server.name, "client": server.client, "status": "skipped",
                 "reason": "stdio probing disabled (needs consent: set probe_stdio)",
                 "server_fingerprint": server.fingerprint},
                server.fingerprint,
            )

    def _run_probe(self, server: RawServer) -> None:
        t0 = time.time()
        res = probe(server, self.cfg.probe_timeout_s)
        self.emitter.emit(
            "mcp_probe",
            "sensor:mcp_probe",
            {"server_name": server.name, "client": server.client, "transport": server.transport,
             "duration_ms": int((time.time() - t0) * 1000), "server_fingerprint": server.fingerprint, **res},
            server.fingerprint,
        )


class _Handler(FileSystemEventHandler):
    def __init__(self, w: McpConfigWatcher):
        self.w = w

    def on_any_event(self, event: FileSystemEvent) -> None:
        for p in (event.src_path, getattr(event, "dest_path", None)):
            if p:
                self.w.schedule(Path(str(p)))


def _stat(path: Path) -> tuple:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return ()
