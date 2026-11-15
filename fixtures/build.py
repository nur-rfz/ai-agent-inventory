"""Build fixtures/signals.jsonl: what the sensors on 6 colleagues' laptops reported.

Every signal body is produced by the real sensor code (parsers, codescan, crontab
parser, process_record, NetAttributor), run over the files in fixtures/colleagues/.
Only the "when" (sensor rollout, daily runs) is scripted here.

Timeline: sensors rolled out by MDM on Mon 2026-10-26; history runs to Fri 2026-11-13.
`scripts/seed.py` shifts everything by whole weeks so the latest event lands just
before "now" on demo day (weekdays and clock times stay realistic).

    python fixtures/build.py
"""

from __future__ import annotations

import json
import random
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from common.catalog import get_catalog  # noqa: E402
from common.redact import leaks, scrub  # noqa: E402
from common.schema import SignalIn, fingerprint  # noqa: E402
from sensor.codescan import scan_file, scan_folder  # noqa: E402
from sensor.netattr import NetAttributor  # noqa: E402
from sensor.parsers import RawServer, parse_data  # noqa: E402
from sensor.paths import ConfigLocation  # noqa: E402
from sensor.process_watcher import process_record  # noqa: E402
from sensor.schedules import parse_crontab, schedule_signals  # noqa: E402

FIX = ROOT / "fixtures"
TZ = ZoneInfo("Europe/Berlin")
ROLLOUT = date(2026, 10, 26)   # Monday
LAST_DAY = date(2026, 11, 13)  # Friday
OUT = FIX / "signals.jsonl"

CLAUDE_DESKTOP = [
    {"pid": 0, "name": "Claude Helper", "exe": "/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper"},
    {"pid": 0, "name": "Claude", "exe": "/Applications/Claude.app/Contents/MacOS/Claude"},
]
CURSOR = [
    {"pid": 0, "name": "Cursor Helper (Plugin)", "exe": "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper (Plugin).app/Contents/MacOS/Cursor Helper (Plugin)"},
    {"pid": 0, "name": "Cursor", "exe": "/Applications/Cursor.app/Contents/MacOS/Cursor"},
]
CRON = [{"pid": 0, "name": "sh", "exe": "/bin/sh"}, {"pid": 0, "name": "cron", "exe": "/usr/sbin/cron"}]
CD_SUPPORT = "Library/Application Support/Claude/claude_desktop_config.json"

# Plausible public IPs for the hosts the processes talk to (attribution runs through
# the real NetAttributor with this as its resolved IP map).
IPS = {
    "gmail.googleapis.com": "142.250.185.69", "oauth2.googleapis.com": "142.250.186.42",
    "graph.microsoft.com": "20.190.159.75", "login.microsoftonline.com": "20.190.160.20",
    "api.hubapi.com": "104.16.252.55", "api.openai.com": "162.159.140.245",
    "api.anthropic.com": "160.79.104.10", "nordlicht.my.salesforce.com": "13.110.53.165",
    "slack.com": "34.226.36.50", "billing-db.internal.nordlicht.example": "10.20.4.12",
}


@dataclass
class Person:
    id: str
    host: str
    user: str
    home: str
    configs: list[tuple[str, str, str, str, str]] = field(default_factory=list)  # (fixture rel, client, fmt, mac rel path, scope)
    code_dirs: list[tuple[str, str]] = field(default_factory=list)                # (fixture rel dir, mac rel dir)
    crontab: str | None = None


PEOPLE = [
    Person("mpatel", "mpatel-mbp", "mpatel", "/Users/mpatel",
           configs=[("claude_desktop_config.json", "claude_desktop", "mcpServers", CD_SUPPORT, "user")]),
    Person("jweber", "jweber-mbp", "jweber", "/Users/jweber",
           configs=[("claude_desktop_config.json", "claude_desktop", "mcpServers", CD_SUPPORT, "user")]),
    Person("praman", "praman-mbp", "praman", "/Users/praman",
           code_dirs=[("ar-automation", "ar-automation")], crontab="crontab.txt"),
    Person("lhoffmann", "lhoffmann-mbp", "lhoffmann", "/Users/lhoffmann",
           code_dirs=[("revops-scripts", "revops-scripts")], crontab="crontab.txt"),
    Person("tbecker", "tbecker-mbp", "tbecker", "/Users/tbecker",
           configs=[("claude_desktop_config.json", "claude_desktop", "mcpServers", CD_SUPPORT, "user")]),
    Person("akim", "akim-mbp", "akim", "/Users/akim",
           configs=[("cursor_mcp.json", "cursor", "mcpServers", ".cursor/mcp.json", "user"),
                    ("billing-service/.mcp.json", "claude_code", "mcpServers", "code/billing-service/.mcp.json", "project")]),
]


def weekdays(start: date, end: date, only: set[int] | None = None):
    d = start
    while d <= end:
        if d.weekday() < 5 and (only is None or d.weekday() in only):
            yield d
        d += timedelta(days=1)


def at(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), TZ)


class Builder:
    def __init__(self):
        self.rows: list[dict] = []
        self.rng = random.Random(7)
        self.net = NetAttributor(get_catalog())
        for h, ip in IPS.items():
            sys_ = get_catalog().lookup_host(h)
            self.net.ip_map[ip] = (h, sys_.name if sys_ else "")
        self.probes = yaml.safe_load((FIX / "probes.yaml").read_text())
        self.pid = 4000
        self.installed: dict[str, datetime] = {}

    def emit(self, p: Person, kind: str, source: str, raw: dict, fp: str, ts: datetime, t: float) -> None:
        clean = scrub(raw)
        assert not leaks(clean), (kind, leaks(clean))
        sig = SignalIn(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"fixture:{p.id}:{len(self.rows)}")),
                       ts=ts.isoformat(timespec="milliseconds"), source=source, host=p.host, user=p.user,
                       kind=kind, raw=clean, fingerprint=fp)
        self.rows.append({"t": round(t, 2), "signal": sig.model_dump()})

    # -- per-person pieces --------------------------------------------------------------
    def configs(self, p: Person, installed: datetime, t: float) -> list[RawServer]:
        servers: list[RawServer] = []
        for rel, client, fmt, mac_rel, scope in p.configs:
            data = json.loads((FIX / "colleagues" / p.id / rel).read_text())
            loc = ConfigLocation(client, Path(p.home) / mac_rel, fmt, scope)
            for s in parse_data(data, loc):
                servers.append(s)
                self.emit(p, "mcp_config", f"sensor:mcp_config:{client}",
                          {**s.public(), "change": "added", "initial": True, "server_fingerprint": s.fingerprint},
                          s.fingerprint, installed, t)
                t += 0.15
                self.probe(p, s, installed + timedelta(seconds=2), t + 0.4)
        return servers

    def probe(self, p: Person, s: RawServer, ts: datetime, t: float) -> None:
        key = s.url if s.transport != "stdio" else next((a for a in s.args if not a.startswith("-")), s.command)
        spec = self.probes.get(key) or {}
        base = {"server_name": s.name, "client": s.client, "transport": s.transport, "server_fingerprint": s.fingerprint}
        if "tools" in spec:
            raw = {**base, "status": "ok", "duration_ms": self.rng.randint(400, 2600),
                   "server_info": spec["server_info"], "instructions": None,
                   "tools": [{"name": n, "description": d, "input_schema": {"type": "object"}} for n, d in spec["tools"]]}
        elif "error" in spec:
            raw = {**base, "status": "error", "duration_ms": self.rng.randint(150, 900), "error": spec["error"]}
        else:
            raw = {**base, "status": "skipped", "reason": spec.get("skipped", "stdio probing disabled (needs consent: set probe_stdio)")}
        self.emit(p, "mcp_probe", "sensor:mcp_probe", raw, s.fingerprint, ts, t)

    def code(self, p: Person, installed: datetime, t: float) -> dict[str, dict]:
        by_script = {}
        for fx_rel, mac_rel in p.code_dirs:
            src = FIX / "colleagues" / p.id / fx_rel
            mac_root = f"{p.home}/{mac_rel}"
            for r in scan_folder(src):
                r = {**r, "path": r["path"].replace(str(src.resolve()), mac_root), "root": mac_root}
                self.emit(p, "code_import", "sensor:codescan", r, fingerprint("script", r["path"]),
                          installed + timedelta(minutes=3), t)
                t += 0.15
                if not r.get("env_file"):
                    by_script[r["path"]] = scan_file(src / r["relative_path"])
        return by_script

    def schedules(self, p: Person, installed: datetime, t: float) -> None:
        if not p.crontab:
            return
        jobs = parse_crontab((FIX / "colleagues" / p.id / p.crontab).read_text(), Path(p.home))
        for raw, fp in schedule_signals(jobs):
            self.emit(p, "schedule", "sensor:schedule", {**raw, "change": "added", "initial": True}, fp,
                      installed + timedelta(seconds=5), t)

    def run(self, p: Person, cmd: list[str], chain: list[dict], servers: list[RawServer], start: datetime,
            end: datetime, hosts: list[tuple[str, int]], t: float, script: str | None = None, sdk: dict | None = None,
            cwd: str | None = None) -> None:
        if start < self.installed[p.id]:  # sensor wasn't there yet
            return
        self.pid += self.rng.randint(3, 400)
        name = Path(cmd[0]).name
        info = {"pid": self.pid, "name": name, "create_time": start.timestamp(), "username": p.user}
        chain = [{**h, "pid": self.pid - 1 - i} for i, h in enumerate(chain)]
        rec = process_record(info, cmd, chain, servers, script, sdk)
        self.emit(p, "process", "sensor:process", {**rec, "initial": False}, rec["fingerprint"], start, t)
        for i, (host, port) in enumerate(hosts):
            attr = self.net.attribute(IPS[host], port)
            self.emit(p, "net_conn", "sensor:net", {
                "pid": self.pid, "name": name, "server_name": rec.get("server_name"), "method": "psutil", **attr,
                "server_fingerprint": rec.get("server_fingerprint")}, rec["fingerprint"],
                start + timedelta(seconds=2 + i), t + 0.05)
        if end:
            self.emit(p, "process", "sensor:process", {**rec, "event": "exited", "initial": False},
                      rec["fingerprint"], end, t + 0.1)

    # -- scenario -----------------------------------------------------------------------
    def build(self) -> None:
        npx = ["node", "/usr/local/lib/node_modules/npm/bin/npx-cli.js", "-y"]
        r = self.rng
        for idx, p in enumerate(PEOPLE):
            t0 = 1.0 + idx * 4.5
            installed = at(ROLLOUT, 9, 2 + idx * 3, r.randint(0, 59))
            self.installed[p.id] = installed
            servers = self.configs(p, installed, t0)
            self.schedules(p, installed, t0 + 0.9)
            sdk_by_script = self.code(p, installed, t0 + 1.0)
            t = t0 + 1.6
            step = 2.6 / 15

            if p.id == "mpatel":
                for d in weekdays(ROLLOUT, LAST_DAY):
                    s = at(d, 8, r.randint(0, 12), r.randint(0, 59))
                    self.run(p, npx + ["@gongrzhe/server-gmail-autoauth-mcp"], CLAUDE_DESKTOP, servers, s,
                             at(d, 17, r.randint(20, 59)), [("gmail.googleapis.com", 443), ("oauth2.googleapis.com", 443)], t)
                    t += step
            elif p.id == "jweber":
                for d in weekdays(ROLLOUT, LAST_DAY):
                    if r.random() < 0.2:  # travelling, no laptop
                        continue
                    s = at(d, 8, r.randint(25, 50), r.randint(0, 59))
                    e = at(d, 18, r.randint(0, 40))
                    self.run(p, npx + ["@softeria/ms-365-mcp-server", "--org-mode"], CLAUDE_DESKTOP, servers, s, e,
                             [("login.microsoftonline.com", 443), ("graph.microsoft.com", 443)], t)
                    self.run(p, npx + ["@hubspot/mcp-server"], CLAUDE_DESKTOP, servers, s + timedelta(seconds=1), e,
                             [("api.hubapi.com", 443)], t + 0.05)
                    t += step
            elif p.id == "praman":
                script = f"{p.home}/ar-automation/summarize_customer_emails.py"
                for d in weekdays(ROLLOUT, LAST_DAY):
                    s = at(d, 7, 30, r.randint(0, 2))
                    self.run(p, ["/usr/bin/python3", "summarize_customer_emails.py"], CRON, servers, s,
                             s + timedelta(seconds=r.randint(70, 140)),
                             [("login.microsoftonline.com", 443), ("graph.microsoft.com", 443), ("api.openai.com", 443)],
                             t, script=script, sdk=sdk_by_script.get(script))
                    t += step
            elif p.id == "lhoffmann":
                script = f"{p.home}/revops-scripts/weekly_pipeline_report.py"
                for d in weekdays(ROLLOUT, LAST_DAY, only={0}):
                    s = at(d, 8, 0, r.randint(0, 3))
                    self.run(p, [f"{p.home}/revops-scripts/.venv/bin/python", "weekly_pipeline_report.py"], CRON, servers,
                             s, s + timedelta(seconds=r.randint(30, 60)),
                             [("nordlicht.my.salesforce.com", 443), ("api.anthropic.com", 443), ("slack.com", 443)],
                             t, script=script, sdk=sdk_by_script.get(script))
                    t += 0.6
            elif p.id == "tbecker":
                for d in weekdays(ROLLOUT, LAST_DAY, only={0, 3}):  # Monday pipeline review, Thursday forecast call
                    s = at(d, 9, r.randint(5, 20), r.randint(0, 59))
                    e = s + timedelta(minutes=r.randint(25, 70))
                    self.run(p, npx + ["@salesforce/mcp", "--orgs", "DEFAULT_TARGET_ORG", "--toolsets", "data,orgs,users"],
                             CLAUDE_DESKTOP, servers, s, e, [("nordlicht.my.salesforce.com", 443)], t)
                    self.run(p, npx + ["@modelcontextprotocol/server-slack"], CLAUDE_DESKTOP, servers,
                             s + timedelta(seconds=1), e, [("slack.com", 443)], t + 0.05)
                    t += 0.4
            elif p.id == "akim":
                for d in weekdays(ROLLOUT, LAST_DAY):
                    s = at(d, 9, r.randint(10, 50), r.randint(0, 59))
                    self.run(p, [f"{p.home}/.local/bin/uvx", "postgres-mcp", "--access-mode=unrestricted"], CURSOR,
                             servers, s, at(d, 19, r.randint(0, 45)),
                             [("billing-db.internal.nordlicht.example", 5432)], t)
                    t += step

        self.rows.sort(key=lambda row: (row["t"], row["signal"]["ts"]))
        with OUT.open("w") as f:
            for row in self.rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        kinds: dict[str, int] = {}
        for row in self.rows:
            kinds[row["signal"]["kind"]] = kinds.get(row["signal"]["kind"], 0) + 1
        print(f"wrote {len(self.rows)} signals to {OUT.relative_to(ROOT)}: {kinds}")


if __name__ == "__main__":
    Builder().build()
