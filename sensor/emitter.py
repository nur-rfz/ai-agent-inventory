"""Build signals and ship them to the backend in the background.

Every signal is scrubbed again right before it is queued, so a bug in a watcher
can't leak a credential.
"""

from __future__ import annotations

import getpass
import logging
import queue
import socket
import threading
import time
from typing import Any

import httpx

from common.redact import leaks, scrub
from common.schema import SignalIn

log = logging.getLogger("sensor.emitter")

HOST = socket.gethostname()
try:
    USER = getpass.getuser()
except Exception:  # pragma: no cover
    USER = "unknown"


class Emitter:
    def __init__(self, backend_url: str, echo: bool = True):
        self.url = backend_url.rstrip("/") + "/api/signals"
        self.echo = echo
        self.q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="emitter", daemon=True)
        self._t.start()

    def emit(self, kind: str, source: str, raw: dict[str, Any], fp: str) -> SignalIn:
        clean = scrub(raw)
        leftovers = leaks(clean)
        if leftovers:  # should never happen: scrub() uses the same patterns
            log.error("secret-shaped data survived scrub in %s; dropping fields", kind)
            clean = {"error": "redaction_guard_tripped", "kind": kind}
        sig = SignalIn(source=source, host=HOST, user=USER, kind=kind, raw=clean, fingerprint=fp)
        if self.echo:
            label = clean.get("server_name") or clean.get("name") or clean.get("system") or clean.get("path") or ""
            extra = clean.get("change") or clean.get("event") or ""
            print(f"  -> {sig.kind:<11} {extra:<8} {label}", flush=True)
        try:
            self.q.put_nowait(sig.model_dump())
        except queue.Full:
            log.warning("emit queue full; dropping signal")
        return sig

    def _run(self) -> None:
        backoff = 0.5
        with httpx.Client(timeout=5.0) as client:
            while not self._stop.is_set():
                try:
                    first = self.q.get(timeout=0.2)
                except queue.Empty:
                    continue
                batch = [first]
                while len(batch) < 200:
                    try:
                        batch.append(self.q.get_nowait())
                    except queue.Empty:
                        break
                while not self._stop.is_set():
                    try:
                        r = client.post(self.url, json=batch)
                        r.raise_for_status()
                        backoff = 0.5
                        break
                    except Exception as e:  # backend down: retry with backoff, keep order
                        log.warning("backend unreachable (%s); retrying in %.1fs", type(e).__name__, backoff)
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 10)

    def flush(self, timeout: float = 5.0) -> None:
        end = time.time() + timeout
        while not self.q.empty() and time.time() < end:
            time.sleep(0.05)
        time.sleep(0.2)

    def stop(self) -> None:
        self.flush()
        self._stop.set()
