"""Map hostnames / ports to business systems using common/systems.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).with_name("systems.yaml")


@dataclass(frozen=True)
class System:
    name: str
    kind: str                    # llm | saas | infra | identity
    domain: str = "business"     # business | engineering


class Catalog:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        data = yaml.safe_load(Path(path).read_text())
        self.exact: dict[str, System] = {}
        self.by_name: dict[str, System] = {}
        self.suffix: list[tuple[str, System]] = []
        for name, spec in (data.get("systems") or {}).items():
            sys_ = System(name, spec.get("kind", "saas"), spec.get("domain", "business"))
            self.by_name[name] = sys_
            for h in spec.get("hosts") or []:
                h = h.lower()
                if h.startswith("*."):
                    self.suffix.append((h[1:], sys_))  # ".atlassian.net"
                else:
                    self.exact[h] = sys_
        self.ports: dict[int, System] = {int(p): System(n, "infra", "engineering") for p, n in (data.get("ports") or {}).items()}
        for sys_ in self.ports.values():
            self.by_name.setdefault(sys_.name, sys_)
        self.suffix.sort(key=lambda t: -len(t[0]))

    def lookup_host(self, host: str | None) -> System | None:
        if not host:
            return None
        host = host.lower().rstrip(".")
        if host in self.exact:
            return self.exact[host]
        for suf, sys_ in self.suffix:
            if host.endswith(suf):
                return sys_
        return None

    def lookup_port(self, port: int | None) -> System | None:
        return self.ports.get(port) if port else None

    def system(self, name: str) -> System:
        return self.by_name.get(name) or System(name, "saas")

    def resolvable_hosts(self) -> list[str]:
        """Exact hostnames we can forward-resolve to build an IP -> system map."""
        return list(self.exact)


@lru_cache(maxsize=4)
def get_catalog(path: str = str(DEFAULT_PATH)) -> Catalog:
    return Catalog(path)


# --- Known MCP packages: systems + default access when a server can't be probed -------

PACKAGES_PATH = Path(__file__).with_name("mcp_packages.yaml")


@lru_cache(maxsize=1)
def mcp_packages() -> dict[str, dict]:
    return yaml.safe_load(PACKAGES_PATH.read_text()) or {}


def package_info(name_or_url: str | None) -> dict | None:
    if not name_or_url:
        return None
    pk = mcp_packages()
    if name_or_url in pk:
        return pk[name_or_url]
    base = name_or_url.split("@")[0] if not name_or_url.startswith("@") else "@" + name_or_url[1:].split("@")[0]
    return pk.get(base)
