"""Scheduled jobs (cron, macOS LaunchAgents) that run scripts: the `triggers` evidence.

Emits `schedule` signals keyed by fingerprint("script", <abs script path>), the same
key codescan and the process watcher use, so they join into one agent.
"""

from __future__ import annotations

import plistlib
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

from common.redact import redact_args
from common.schema import fingerprint

from .emitter import Emitter
from .paths import current_os

SCRIPT_EXT = (".py", ".js", ".mjs", ".ts", ".sh", ".rb")
CRON_LINE = re.compile(r"^\s*((?:@\w+)|(?:\S+\s+\S+\s+\S+\s+\S+\s+\S+))\s+(.+)$")


def _expand(p: str, home: Path) -> Path:
    return Path(str(home) + p[1:]) if p.startswith("~") else Path(p)


def scripts_in_command(command: str, home: Path) -> list[str]:
    """Find script paths in a shell command, honouring a leading `cd DIR &&`."""
    cwd: Path | None = None
    found: list[str] = []
    for part in re.split(r"&&|;|\|\|", command):
        try:
            toks = shlex.split(part.split(">>")[0].split(">")[0])
        except ValueError:
            continue
        if not toks:
            continue
        if toks[0] == "cd" and len(toks) > 1:
            cwd = _expand(toks[1], home)
            continue
        for t in toks:
            if t.endswith(SCRIPT_EXT):
                p = _expand(t, home)
                if not p.is_absolute() and cwd:
                    p = cwd / p
                found.append(str(p))
    return found


def parse_crontab(text: str, home: Path) -> list[dict]:
    jobs = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or re.match(r"^\s*\w+=", line):
            continue
        m = CRON_LINE.match(line)
        if not m:
            continue
        expr, cmd = m.group(1), m.group(2)
        scripts = scripts_in_command(cmd, home)
        if scripts:
            jobs.append({"scheduler": "cron", "cron": expr, "command": " ".join(redact_args(shlex.split(cmd, posix=True))
                         if _splittable(cmd) else [cmd]), "scripts": scripts})
    return jobs


def _splittable(cmd: str) -> bool:
    try:
        shlex.split(cmd)
        return True
    except ValueError:
        return False


def parse_launchagent(path: Path, home: Path) -> dict | None:
    try:
        data = plistlib.loads(path.read_bytes())
    except Exception:
        return None
    args = data.get("ProgramArguments") or ([data["Program"]] if data.get("Program") else [])
    scripts = scripts_in_command(" ".join(shlex.quote(a) for a in args), home)
    if not scripts:
        return None
    cal = data.get("StartCalendarInterval")
    cron = None
    if isinstance(cal, dict):
        cron = f"{cal.get('Minute', '*')} {cal.get('Hour', '*')} {cal.get('Day', '*')} {cal.get('Month', '*')} {cal.get('Weekday', '*')}"
    return {"scheduler": "launchd", "label": data.get("Label"), "cron": cron,
            "interval_s": data.get("StartInterval"), "command": " ".join(redact_args(args)), "scripts": scripts}


def collect(home: Path | None = None) -> list[dict]:
    home = home or Path.home()
    jobs: list[dict] = []
    if shutil.which("crontab"):
        try:
            out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=3)
            if out.returncode == 0:
                jobs += parse_crontab(out.stdout, home)
        except (subprocess.SubprocessError, OSError):
            pass
    if current_os() == "macos":
        for p in sorted((home / "Library" / "LaunchAgents").glob("*.plist")):
            if j := parse_launchagent(p, home):
                jobs.append(j)
    return jobs


def schedule_signals(jobs: list[dict]) -> list[tuple[dict, str]]:
    """(raw, fingerprint) per (job, script)."""
    out = []
    for j in jobs:
        for s in j["scripts"]:
            out.append(({**{k: v for k, v in j.items() if k != "scripts"}, "script": s, "name": Path(s).name},
                        fingerprint("script", s)))
    return out


class ScheduleWatcher:
    def __init__(self, emitter: Emitter, poll_s: float = 60):
        self.emitter = emitter
        self.poll_s = poll_s
        self.prev: dict[str, dict] = {}
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="schedules", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        first = True
        while not self._stop.is_set():
            cur = {fp + raw.get("cron", "") + str(raw.get("interval_s")): (raw, fp)
                   for raw, fp in schedule_signals(collect())}
            for k, (raw, fp) in cur.items():
                if k not in self.prev:
                    self.emitter.emit("schedule", "sensor:schedule", {**raw, "change": "added", "initial": first}, fp)
            for k, (raw, fp) in self.prev.items():
                if k not in cur:
                    self.emitter.emit("schedule", "sensor:schedule", {**raw, "change": "removed", "initial": False}, fp)
            self.prev = cur
            first = False
            self._stop.wait(self.poll_s)
