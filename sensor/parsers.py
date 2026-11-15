"""Parse MCP config files into normalized server entries.

`parse_file()` returns RawServer objects that still hold real env/header values
(needed in-process for probing). Use `RawServer.public()` for anything that
leaves the sensor: it is redacted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from common.redact import redact_args, redact_mapping_values, redact_url
from common.schema import fingerprint

from .paths import ConfigLocation


@dataclass
class RawServer:
    name: str
    client: str
    config_path: str
    scope: str                     # user | project | local (claude code per-project in ~/.claude.json)
    transport: str                 # stdio | http | sse
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)       # REAL values: never emit
    headers: dict[str, str] = field(default_factory=dict)   # REAL values: never emit
    project: str | None = None
    disabled: bool = False

    @property
    def fingerprint(self) -> str:
        """Identity of the server itself (what it runs), independent of which file lists it."""
        if self.transport == "stdio":
            return fingerprint("stdio", self.command, redact_args(self.args))
        return fingerprint("remote", redact_url(self.url or ""))

    def hostnames(self) -> list[str]:
        """Hostnames this server is configured to talk to (from url / URL-ish env values).

        Hostnames are not credentials; they let us map reach (e.g. acme.atlassian.net -> Jira).
        """
        hosts: set[str] = set()
        candidates = [self.url or ""] + list(self.env.values()) + list(self.args)
        for c in candidates:
            for m in re.finditer(r"[a-z][a-z0-9+.\-]*://[^\s\"']+", str(c), re.I):
                try:
                    h = urlsplit(m.group(0)).hostname
                except ValueError:
                    h = None
                if h:
                    hosts.add(h.lower())
        return sorted(hosts)

    def public(self) -> dict[str, Any]:
        return {
            "server_name": self.name,
            "client": self.client,
            "config_path": self.config_path,
            "scope": self.scope,
            "project": self.project,
            "transport": self.transport,
            "command": self.command,
            "args": redact_args(self.args),
            "url": redact_url(self.url) if self.url else None,
            "env": redact_mapping_values(self.env),
            "env_keys": sorted(self.env),
            "header_keys": sorted(self.headers),
            "hostnames": self.hostnames(),
            "disabled": self.disabled,
        }


# --- JSONC (VS Code allows comments + trailing commas) ---------------------

def loads_jsonc(text: str) -> Any:
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(c)
            i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned) if cleaned.strip() else {}


# --- per-format extraction ----------------------------------------------------

def _entry_to_server(name: str, e: dict[str, Any], loc: ConfigLocation, scope: str, project: str | None) -> RawServer | None:
    if not isinstance(e, dict):
        return None
    url = e.get("url") or e.get("serverUrl") or e.get("httpUrl")
    typ = str(e.get("type") or e.get("transport") or "").lower()
    if url:
        transport = "sse" if typ == "sse" or str(url).rstrip("/").endswith("/sse") else "http"
    else:
        transport = "stdio"
    if transport == "stdio" and not e.get("command"):
        return None
    env = e.get("env") or {}
    headers = e.get("headers") or {}
    return RawServer(
        name=name,
        client=loc.client,
        config_path=str(loc.path),
        scope=scope,
        transport=transport,
        command=e.get("command"),
        args=[str(a) for a in (e.get("args") or [])],
        url=url,
        env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
        headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
        project=project,
        disabled=bool(e.get("disabled", False)),
    )


def _servers_from(mapping: Any, loc: ConfigLocation, scope: str, project: str | None = None) -> list[RawServer]:
    if not isinstance(mapping, dict):
        return []
    out = []
    for name, entry in mapping.items():
        s = _entry_to_server(str(name), entry, loc, scope, project)
        if s:
            out.append(s)
    return out


def parse_data(data: Any, loc: ConfigLocation) -> list[RawServer]:
    if not isinstance(data, dict):
        return []
    project = str(loc.path.parent.parent if loc.path.parent.name in (".cursor", ".vscode") else loc.path.parent) \
        if loc.scope == "project" else None
    if loc.fmt == "mcpServers":
        return _servers_from(data.get("mcpServers"), loc, loc.scope, project)
    if loc.fmt == "vscode_servers":
        return _servers_from(data.get("servers"), loc, loc.scope, project)
    if loc.fmt == "vscode_settings":
        mcp = data.get("mcp") or {}
        return _servers_from(mcp.get("servers") if isinstance(mcp, dict) else None, loc, "user")
    if loc.fmt == "claude_code_user":
        servers = _servers_from(data.get("mcpServers"), loc, "user")
        for proj, cfg in (data.get("projects") or {}).items():
            if isinstance(cfg, dict):
                servers += _servers_from(cfg.get("mcpServers"), loc, "local", proj)
        return servers
    return []


def parse_file(loc: ConfigLocation) -> list[RawServer] | None:
    """None = file missing/unreadable/half-written (keep previous state); [] = no servers."""
    try:
        text = loc.path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return []
    except OSError:
        return None
    try:
        data = loads_jsonc(text)
    except (json.JSONDecodeError, ValueError):
        return None  # editor mid-save; wait for the next event
    return parse_data(data, loc)


def claude_code_projects(path: Path) -> list[str]:
    """Project roots Claude Code knows about: free discovery of folders to watch."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [p for p in (data.get("projects") or {}) if isinstance(p, str)]
