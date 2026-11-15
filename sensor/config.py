"""Sensor settings: sensor.yaml at the repo root (optional) + env overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class SensorConfig:
    backend_url: str = "http://127.0.0.1:8787"
    # Spawning a stdio MCP server runs third-party code on this machine. Off by default;
    # turn on only with explicit consent, optionally limited to an allowlist of server names.
    probe_stdio: bool = False
    probe_stdio_allowlist: list[str] = field(default_factory=list)
    probe_timeout_s: float = 5.0
    debounce_s: float = 0.5
    config_poll_s: float = 1.0       # safety net next to watchdog (missing dirs, atomic renames)
    process_poll_s: float = 2.0
    project_roots: list[str] = field(default_factory=list)
    catalog_path: str = str(REPO_ROOT / "common" / "systems.yaml")
    echo: bool = True                # print a one-line summary of every emitted signal

    @classmethod
    def load(cls, path: Path | None = None) -> "SensorConfig":
        path = path or Path(os.environ.get("SENSOR_CONFIG", REPO_ROOT / "sensor.yaml"))
        data = {}
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        if v := os.environ.get("BACKEND_URL"):
            cfg.backend_url = v
        if v := os.environ.get("SENSOR_PROBE_STDIO"):
            cfg.probe_stdio = v.lower() in ("1", "true", "yes")
        if v := os.environ.get("SENSOR_PROJECT_ROOTS"):
            cfg.project_roots += [p for p in v.split(os.pathsep) if p]
        return cfg
