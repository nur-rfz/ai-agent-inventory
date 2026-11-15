"""Where each AI client keeps its MCP configuration, per OS.

Sources (checked Nov 2026; re-verify with `python -m sensor.paths`):
- Claude Desktop: macOS ~/Library/Application Support/Claude/claude_desktop_config.json,
  Windows %APPDATA%\\Claude\\claude_desktop_config.json. No official Linux build;
  community builds use ~/.config/Claude/claude_desktop_config.json.
- Cursor: ~/.cursor/mcp.json (global, all OSes) and <project>/.cursor/mcp.json.
- VS Code: dedicated <user dir>/mcp.json (since 1.102) plus the legacy "mcp" key
  in <user dir>/settings.json, and <project>/.vscode/mcp.json.
  User dir: macOS ~/Library/Application Support/Code/User, Linux ~/.config/Code/User,
  Windows %APPDATA%\\Code\\User. Insiders uses "Code - Insiders".
- Windsurf: ~/.codeium/windsurf/mcp_config.json.
- Claude Code: ~/.claude.json (top-level "mcpServers" = user scope,
  "projects"[path]["mcpServers"] = local scope) and <project>/.mcp.json.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Client = Literal["claude_desktop", "cursor", "vscode", "windsurf", "claude_code"]
Fmt = Literal["mcpServers", "vscode_servers", "vscode_settings", "claude_code_user"]


@dataclass(frozen=True)
class ConfigLocation:
    client: Client
    path: Path
    fmt: Fmt
    scope: Literal["user", "project"]
    note: str = ""


def current_os() -> Literal["macos", "linux", "windows"]:
    s = platform.system()
    if s == "Darwin":
        return "macos"
    if s == "Windows":
        return "windows"
    return "linux"


def _app_support(home: Path) -> Path:
    os_ = current_os()
    if os_ == "macos":
        return home / "Library" / "Application Support"
    if os_ == "windows":
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))


def user_locations(home: Path | None = None) -> list[ConfigLocation]:
    home = home or Path.home()
    support = _app_support(home)
    os_ = current_os()
    locs = [
        ConfigLocation(
            "claude_desktop",
            support / "Claude" / "claude_desktop_config.json",
            "mcpServers",
            "user",
            "unofficial Linux build" if os_ == "linux" else "",
        ),
        ConfigLocation("cursor", home / ".cursor" / "mcp.json", "mcpServers", "user"),
        ConfigLocation("windsurf", home / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers", "user"),
        ConfigLocation("claude_code", home / ".claude.json", "claude_code_user", "user"),
    ]
    for flavour in ("Code", "Code - Insiders"):
        udir = support / flavour / "User"
        locs.append(ConfigLocation("vscode", udir / "mcp.json", "vscode_servers", "user", flavour))
        locs.append(ConfigLocation("vscode", udir / "settings.json", "vscode_settings", "user", f"{flavour} legacy 'mcp' key"))
    return locs


def project_locations(root: Path) -> list[ConfigLocation]:
    return [
        ConfigLocation("cursor", root / ".cursor" / "mcp.json", "mcpServers", "project"),
        ConfigLocation("vscode", root / ".vscode" / "mcp.json", "vscode_servers", "project"),
        ConfigLocation("claude_code", root / ".mcp.json", "mcpServers", "project"),
    ]


def report(home: Path | None = None) -> str:
    lines = [f"OS detected: {current_os()} ({platform.platform()}), python {sys.version.split()[0]}", ""]
    for loc in user_locations(home):
        mark = "FOUND " if loc.path.exists() else "absent"
        lines.append(f"  [{mark}] {loc.client:<15} {loc.path}  {('(' + loc.note + ')') if loc.note else ''}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
