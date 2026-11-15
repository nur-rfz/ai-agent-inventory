"""Attribute a process's remote connections to business systems.

psutil gives IPs, not hostnames, so we forward-resolve the catalog's exact
hostnames (plus hostnames seen in MCP configs) into an IP -> system map, then
fall back to cached reverse DNS for wildcard entries (e.g. *.atlassian.net).
Without root on macOS, per-process connection listing can fail; we then try
`lsof`, and if that fails too we report `unavailable` instead of crashing.
"""

from __future__ import annotations

import ipaddress
import logging
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psutil

from common.catalog import Catalog

log = logging.getLogger("sensor.net")


class NetAttributor:
    def __init__(self, catalog: Catalog, refresh_s: float = 300):
        self.catalog = catalog
        self.refresh_s = refresh_s
        self.ip_map: dict[str, tuple[str, str]] = {}   # ip -> (hostname, system)
        self.extra_hosts: set[str] = set()
        self._rdns: dict[str, str | None] = {}
        self._lock = threading.Lock()
        self._last = 0.0
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dns")

    def add_hosts(self, hosts: list[str]) -> None:
        new = set(hosts) - self.extra_hosts
        if new:
            self.extra_hosts |= new
            self._pool.submit(self._resolve_many, list(new))

    def maybe_refresh(self) -> None:
        if time.time() - self._last > self.refresh_s:
            self._last = time.time()
            self._pool.submit(self._resolve_many, self.catalog.resolvable_hosts() + list(self.extra_hosts))

    def _resolve_many(self, hosts: list[str]) -> None:
        for h in hosts:
            sys_ = self.catalog.lookup_host(h)
            try:
                infos = socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)
            except OSError:
                continue
            with self._lock:
                for info in infos:
                    self.ip_map[info[4][0]] = (h, sys_.name if sys_ else "")

    def attribute(self, ip: str, port: int) -> dict:
        with self._lock:
            hit = self.ip_map.get(ip)
        host = hit[0] if hit else self._reverse(ip)
        sys_ = self.catalog.lookup_host(host) if host else None
        if not sys_:
            sys_ = self.catalog.lookup_port(port)
        try:
            private = ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback
        except ValueError:
            private = False
        return {"ip": ip, "port": port, "hostname": host, "system": sys_.name if sys_ else None,
                "system_kind": sys_.kind if sys_ else None, "private": private}

    def _reverse(self, ip: str) -> str | None:
        if ip in self._rdns:
            return self._rdns[ip]
        self._rdns[ip] = None  # resolve in background; next poll will see it
        def work():
            try:
                self._rdns[ip] = socket.gethostbyaddr(ip)[0]
            except OSError:
                pass
        self._pool.submit(work)
        return None


def remote_endpoints(pid: int) -> tuple[list[tuple[str, int]], str]:
    """Return ([(ip, port)], method). method: psutil | lsof | unavailable."""
    try:
        conns = psutil.Process(pid).net_connections(kind="inet")
        # outbound only: drop accepted connections on ports this process listens on
        listening = {c.laddr.port for c in conns if c.status == psutil.CONN_LISTEN}
        return sorted({(c.raddr.ip, c.raddr.port) for c in conns
                       if c.raddr and c.laddr.port not in listening}), "psutil"
    except (psutil.AccessDenied, psutil.ZombieProcess):
        pass
    except psutil.NoSuchProcess:
        return [], "gone"
    if shutil.which("lsof"):
        try:
            out = subprocess.run(["lsof", "-a", "-p", str(pid), "-i", "-n", "-P", "-F", "n"],
                                 capture_output=True, text=True, timeout=3).stdout
            eps = set()
            listening = {l.rsplit(":", 1)[-1] for l in out.splitlines() if l.startswith("n") and "->" not in l}
            for line in out.splitlines():
                if line.startswith("n") and "->" in line:
                    local, remote = line[1:].split("->", 1)
                    if local.rsplit(":", 1)[-1] in listening:
                        continue
                    host, _, port = remote.rpartition(":")
                    host = host.strip("[]")
                    if port.isdigit():
                        eps.add((host, int(port)))
            return sorted(eps), "lsof"
        except (subprocess.SubprocessError, OSError):
            pass
    return [], "unavailable"
