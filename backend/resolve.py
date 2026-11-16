"""Signals -> Agents (deterministic part of Phase 3).

1. Components: everything known about one thing on one host, keyed by the shared
   fingerprint: an MCP server in a config file (+ its probe, processes, connections),
   a script (+ code scan, cron job, runs, connections), an OAuth app or an LLM vendor
   project (identity logs).
2. Agents: MCP servers in one client config are split by purpose: engineering
   servers (GitHub, Postgres, Sentry, ...) become a coding agent and everything
   else a business agent, so adding a business MCP server to a Cursor config
   that already has GitHub yields a separate agent. Each script is one agent.
3. Identity events are linked to an agent of the same owner when the app name or
   the systems match; otherwise they become a SaaS agent of their own.
4. Reach, triggers, risk and a rule-based classification come from the evidence.
   Claude enrichment (backend/enrich.py) refines name, purpose and classification;
   it never removes evidence-backed facts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable

from common.catalog import get_catalog, package_info

from .org import Directory, Person, Policy

CODING_CLIENTS = {"cursor", "claude_code", "vscode", "windsurf"}
CLIENT_LABEL = {"cursor": "Cursor", "claude_code": "Claude Code", "vscode": "VS Code", "windsurf": "Windsurf",
                "claude_desktop": "Claude Desktop", "script": "Script", "saas": "SaaS app"}
WRITE_VERB = re.compile(r"(^|[_\-\s])(send|post|create|update|delete|remove|modify|merge|write|upsert|insert|"
                        r"assign|draft|reply|move|add|set|batch[_\-]modify|batch[_\-]update|deploy|patch|put)", re.I)
READ_VERB = re.compile(r"(^|[_\-\s])(get|list|search|read|query|fetch|find|lookup|run_soql|summari[sz]e|describe)", re.I)
# Keywords in tool names/descriptions that name a system.
SYSTEM_WORDS = {
    "gmail": "Gmail", "outlook": "Microsoft 365", "microsoft graph": "Microsoft 365", "mail-message": "Microsoft 365",
    "calendar": None, "hubspot": "HubSpot", "salesforce": "Salesforce", "soql": "Salesforce", "slack": "Slack",
    "github": "GitHub", "pull request": "GitHub", "repository": "GitHub", "jira": "Jira", "confluence": "Confluence",
    "notion": "Notion", "linear": "Linear", "sentry": "Sentry", "postgres": "Postgres", "sql": "Postgres",
    "zendesk": "Zendesk", "google drive": "Google Drive", "sharepoint": "Microsoft 365", "onedrive": "Microsoft 365",
    "stripe": "Stripe",
}
# Code calls that write (method name -> system).
WRITE_CALLS = {"chat_postMessage": "Slack", "files_upload_v2": "Slack", "files_upload": "Slack", "sendMail": "Microsoft 365",
               "send_mail": "Microsoft 365", "send_message": "Gmail", "create_issue": "GitHub"}
SCOPE_SYSTEMS = [  # (scope regex, system, access)
    (r"gmail\.(modify|send|compose)", "Gmail", "rw"), (r"gmail\.readonly", "Gmail", "r"),
    (r"^Mail\.(ReadWrite|Send)", "Microsoft 365", "rw"), (r"^Mail\.Read", "Microsoft 365", "r"),
    (r"^Files\.Read", "Microsoft 365", "r"), (r"^Sites\.Read", "Microsoft 365", "r"),
    (r"^Files\.ReadWrite", "Microsoft 365", "rw"), (r"^Calendars\.ReadWrite", "Microsoft 365", "rw"),
    (r"^Calendars\.Read", "Microsoft 365", "r"), (r"^OnlineMeetings", "Microsoft 365", "r"),
    (r"drive", "Google Drive", "r"),
]
SENSITIVE_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE|_URI$|_DSN$|_URL$)", re.I)
SEVERITY = {"low": 1, "medium": 2, "high": 3}


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _merge_access(a: str | None, b: str) -> str:
    s = set(a or "") | set(b)
    return "rw" if {"r", "w"} <= s else ("w" if "w" in s else "r")


def _tokens(s: str | None) -> set[str]:
    stop = {"server", "the", "app", "project", "personal", "and", "for", "of", "a", "mcp", "ai"}
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 1 and t not in stop}


@dataclass
class Reach:
    access: str = "r"
    evidence: set[str] = field(default_factory=set)
    via: set[str] = field(default_factory=set)

    def add(self, access: str, sig_id: str | None, via: str) -> None:
        self.access = _merge_access(self.access if (self.evidence or self.via) else None, access)
        if sig_id:
            self.evidence.add(sig_id)
        self.via.add(via)


@dataclass
class Component:
    key: str
    kind: str                       # mcp_server | script | oauth_app | llm_project
    host: str
    user: str
    fingerprint: str
    signals: list[dict] = field(default_factory=list)

    # filled by analyse()
    name: str = ""
    client: str | None = None
    bundle: tuple | None = None
    removed: bool = False
    domain: str = "business"
    tools: list[dict] = field(default_factory=list)
    reach: dict[str, Reach] = field(default_factory=lambda: defaultdict(Reach))
    llm: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    starts: list[datetime] = field(default_factory=list)
    schedules: list[dict] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    owner_email: str | None = None

    @property
    def ids(self) -> list[str]:
        return [s["id"] for s in self.signals]

    def of(self, kind: str) -> list[dict]:
        return [s for s in self.signals if s["kind"] == kind]


# --------------------------------------------------------------------------------------
# 1. components
# --------------------------------------------------------------------------------------

def build_components(signals: Iterable[dict]) -> list[Component]:
    sigs = sorted(signals, key=lambda s: s["ts"])
    comps: dict[str, Component] = {}
    by_host_fp: dict[tuple[str, str], list[Component]] = defaultdict(list)

    def get(key, kind, s, fp=None):
        if key not in comps:
            comps[key] = Component(key, kind, s["host"], s["user"], fp or s["fingerprint"])
            by_host_fp[(s["host"], fp or s["fingerprint"])].append(comps[key])
        return comps[key]

    # pass 1: anchors (configs, scripts, identity)
    for s in sigs:
        k, r = s["kind"], s["raw"]
        if k == "mcp_config":
            get(f"mcp|{s['host']}|{s['fingerprint']}|{r.get('config_path')}|{r.get('project') or ''}",
                "mcp_server", s).signals.append(s)
        elif k in ("schedule",) or (k == "code_import" and r.get("sdks")):
            get(f"script|{s['host']}|{s['fingerprint']}", "script", s).signals.append(s)
        elif k == "oauth_grant":
            get(f"oauth|{s['fingerprint']}", "oauth_app", s).signals.append(s)
        elif k == "api_key":
            get(f"llm|{s['fingerprint']}", "llm_project", s).signals.append(s)

    # pass 2: attach probes / processes / connections / .env files
    for s in sigs:
        k, r = s["kind"], s["raw"]
        if k in ("mcp_probe", "process", "net_conn"):
            targets = by_host_fp.get((s["host"], s["fingerprint"]))
            if not targets and k == "process" and r.get("category") == "ai_script" and r.get("script"):
                targets = [get(f"script|{s['host']}|{s['fingerprint']}", "script", s)]
            for c in targets or []:
                c.signals.append(s)
        elif k == "code_import" and not r.get("sdks"):  # .env next to a script: same folder
            for c in comps.values():
                if c.kind == "script" and c.host == s["host"] and any(
                        x["raw"].get("root") == r.get("root") for x in c.of("code_import")):
                    c.signals.append(s)
    for c in comps.values():
        c.signals.sort(key=lambda s: s["ts"])
    return list(comps.values())


# --------------------------------------------------------------------------------------
# 2. per-component facts
# --------------------------------------------------------------------------------------

def _tool_system(tool: dict, default: str | None) -> str | None:
    hay = f"{tool['name']} {tool.get('description') or ''}".lower()
    for word, system in SYSTEM_WORDS.items():
        if word in hay and system:
            return system
    return default


def _tool_access(tool: dict) -> str:
    name = tool["name"]
    if WRITE_VERB.search(name):
        return "w"
    if READ_VERB.search(name):
        return "r"
    return "w" if WRITE_VERB.search(tool.get("description") or "") else "r"


def analyse(c: Component) -> None:
    cat = get_catalog()
    if c.kind == "mcp_server":
        cfgs = c.of("mcp_config")
        last = cfgs[-1]["raw"]
        c.removed = last.get("change") == "removed"
        c.client = last.get("client")
        c.name = last.get("server_name")
        c.bundle = (c.host, c.client, last.get("config_path"), last.get("project") or "")
        pkg = last.get("url") or next((a for a in last.get("args") or [] if not a.startswith("-")), last.get("command"))
        info = package_info(pkg) or {}
        c.facts.update({"transport": last.get("transport"), "package": pkg, "package_label": info.get("label"),
                        "config_path": last.get("config_path"), "project": last.get("project"),
                        "env_keys": last.get("env_keys") or [], "header_keys": last.get("header_keys") or [],
                        "args": last.get("args") or [], "url": last.get("url"), "scope": last.get("scope")})
        default_sys = next(iter(info.get("systems", {})), None)
        for sysname, acc in (info.get("systems") or {}).items():
            flag_acc = next((v for f, v in (info.get("access_flag") or {}).items() if f in (last.get("args") or [])), None)
            c.reach[sysname].add(flag_acc or acc, cfgs[-1]["id"], f"known package {pkg}")
        for h in last.get("hostnames") or []:
            sys_ = cat.lookup_host(h)
            if sys_ and sys_.kind not in ("llm", "identity"):
                c.reach[sys_.name].add("r", cfgs[-1]["id"], f"configured host {h}")
        probes = c.of("mcp_probe")
        if probes:
            p = probes[-1]["raw"]
            c.facts["probe"] = {k: p.get(k) for k in ("status", "error", "reason", "server_info", "instructions")}
            if p.get("status") == "ok":
                tool_systems: dict[str, str] = {}
                for t in p.get("tools") or []:
                    sysname = _tool_system(t, default_sys)
                    acc = _tool_access(t)
                    c.tools.append({"name": t["name"], "description": t.get("description"), "server": c.name,
                                    "system": sysname, "access": acc})
                    if sysname:
                        tool_systems[sysname] = _merge_access(tool_systems.get(sysname), acc)
                for sysname, acc in tool_systems.items():  # tools are the best evidence: replace package default
                    fresh = sysname not in c.reach
                    r = c.reach[sysname]
                    if fresh or ("known package" in " ".join(r.via) and not any("tool" in v for v in r.via)):
                        r.access = acc
                    else:
                        r.access = _merge_access(r.access, acc)
                    r.evidence.add(probes[-1]["id"])
                    r.via.add("tools/list")
    elif c.kind == "script":
        code = [s for s in c.of("code_import") if s["raw"].get("sdks")]
        sch = c.of("schedule")
        procs = c.of("process")
        anchor = (code or sch or procs)[0]["raw"]
        path = anchor.get("path") or anchor.get("script")
        c.name = path.rsplit("/", 1)[-1] if path else "script"
        c.client = "script"
        c.bundle = (c.host, "script", path, "")
        c.facts["path"] = path
        for s in code:
            r = s["raw"]
            c.facts.update({"sdks": r.get("sdks"), "docstring": r.get("docstring"), "prompts": r.get("prompts"),
                            "calls": r.get("calls"), "env_names": r.get("env_names")})
            for sysname in r.get("systems") or []:
                c.reach[sysname].add("r", s["id"], "SDK import")
            for call in r.get("calls") or []:
                if call in WRITE_CALLS:
                    c.reach[WRITE_CALLS[call]].add("w", s["id"], f"calls {call}()")
            for sdk in r.get("sdks") or []:
                prov = {"openai": "OpenAI", "anthropic": "Anthropic", "google_genai": "Google Gemini",
                        "mistral": "Mistral", "cohere": "Cohere"}.get(sdk)
                if prov:
                    c.llm[prov].add(s["id"])
        env_names = sorted({n for s in c.of("code_import") for n in s["raw"].get("env_names") or []})
        c.facts["env_names"] = env_names
        for s in sch:
            if s["raw"].get("change") != "removed":
                c.schedules.append({"cron": s["raw"].get("cron"), "scheduler": s["raw"].get("scheduler"), "id": s["id"]})
    elif c.kind in ("oauth_app", "llm_project"):
        for s in c.signals:
            r = s["raw"]
            if r.get("event") == "app_registered":
                c.owner_email = r.get("user_email")
            if r.get("event") in ("consent", "api_key.created", "project.created") and not c.owner_email:
                c.owner_email = r.get("user_email")
            for sc in r.get("scopes") or []:
                for pat, sysname, acc in SCOPE_SYSTEMS:
                    if re.search(pat, sc.split("/")[-1], re.I):
                        c.reach[sysname].add(acc, s["id"], f"OAuth scope {sc.split('/')[-1]}")
                        break
        r0 = c.signals[0]["raw"]
        c.name = r0.get("app_name") or r0.get("project_name") or "unknown app"
        c.client = "saas"
        c.facts.update({"provider": r0.get("provider"), "app_id": r0.get("app_id") or r0.get("project_id")})
        if c.kind == "llm_project":
            c.llm["OpenAI" if r0.get("provider") == "openai" else r0.get("provider", "").title()].update(c.ids)
        c.bundle = ("identity", c.key, "", "")
        c.host = r0.get("provider", c.host)

    # processes + network apply to every kind that has them
    for s in c.of("process"):
        if s["raw"].get("event") == "started" and s["raw"].get("started_at"):
            c.starts.append(datetime.fromtimestamp(s["raw"]["started_at"], timezone.utc).astimezone(_ts(s["ts"]).tzinfo))
        if s["raw"].get("surface"):
            c.facts["surface_seen"] = s["raw"]["surface"]
    for s in c.of("net_conn"):
        r = s["raw"]
        if not r.get("system"):
            continue
        kind = r.get("system_kind")
        if kind == "llm":
            c.llm[r["system"]].add(s["id"])
        elif kind != "identity":
            c.reach[r["system"]].add("r", s["id"], f"connects to {r.get('hostname') or r.get('ip')}:{r.get('port')}")
    # domain: engineering if every reached system is engineering (or coding client with nothing known)
    systems = [cat.system(n) for n in c.reach]
    if systems:
        c.domain = "engineering" if all(s.domain == "engineering" for s in systems) else "business"
    else:
        c.domain = "engineering" if c.client in CODING_CLIENTS else "business"


# --------------------------------------------------------------------------------------
# 3. triggers
# --------------------------------------------------------------------------------------

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def cron_text(expr: str | None) -> str:
    if not expr:
        return "scheduled"
    parts = expr.split()
    if len(parts) != 5:
        return expr
    mi, hr, dom, mon, dow = parts
    when = f"{int(hr):02d}:{int(mi):02d}" if hr.isdigit() and mi.isdigit() else f"{hr}:{mi}"
    names = {"1-5": "Mon–Fri", "*": "every day", "0,6": "weekends"}
    if dow in names:
        days = names[dow]
    elif dow.isdigit():
        days = DAYS[(int(dow) - 1) % 7] if dow != "0" else "Sun"
    else:
        days = f"days {dow}"
    return f"{days} {when}" if dom == "*" and mon == "*" else expr


def recurrence(starts: list[datetime]) -> dict | None:
    if len(starts) < 2:
        return None
    days = Counter(d.weekday() for d in starts)
    mins = round(median(d.hour * 60 + d.minute for d in starts) / 15) * 15  # stable across new runs
    hhmm = f"~{int(mins // 60):02d}:{int(mins % 60):02d}"
    weeks = max(1, (max(starts) - min(starts)).days / 7)
    regular = [d for d, n in days.items() if n / weeks >= 0.5]
    if set(regular) >= {0, 1, 2, 3, 4} or len(regular) >= 4:
        pattern = "weekdays"
    elif regular:
        pattern = " & ".join(DAYS[d] for d in sorted(regular))
    else:
        pattern = "occasionally"
    return {"pattern": pattern, "time": hhmm, "runs": len(starts)}


def triggers_for(comps: list[Component], surface: str) -> list[dict]:
    out = []
    for c in comps:
        for sch in c.schedules:
            out.append({"type": "schedule", "cron": sch["cron"], "description": f"{sch['scheduler']}: {cron_text(sch['cron'])}",
                        "evidence_ids": [sch["id"]], "source": "evidence"})
    if not out:
        starts = sorted(d for c in comps for d in c.starts)
        rec = recurrence(starts)
        if rec:
            how = "interactive" if surface in CODING_CLIENTS | {"claude_desktop"} else "run"
            proc_ids = [s["id"] for c in comps for s in c.of("process")][:10]
            out.append({"type": "manual", "cron": None, "source": "evidence", "evidence_ids": proc_ids,
                        "description": f"{how} in {CLIENT_LABEL.get(surface, surface)}, {rec['pattern']} {rec['time']} "
                                       f"({rec['runs']} runs seen)"})
    return out


# --------------------------------------------------------------------------------------
# 4. agents
# --------------------------------------------------------------------------------------

def _stable_id(*parts: Any) -> str:
    return "ag_" + hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:12]


@dataclass
class AgentDraft:
    id: str
    surface: str
    domain: str
    owner: Person
    host: str
    members: list[Component]
    linked: list[Component] = field(default_factory=list)


def group_agents(comps: list[Component], directory: Directory) -> list[AgentDraft]:
    drafts: dict[str, AgentDraft] = {}
    identity: list[Component] = []
    for c in comps:
        if c.kind in ("oauth_app", "llm_project"):
            identity.append(c)
            continue
        if c.removed:
            continue
        if c.kind == "script":
            aid = _stable_id("script", c.host, c.facts.get("path"))
            surface = "script"
        else:
            aid = _stable_id("mcp", *c.bundle, c.domain)
            surface = c.client or "unknown"
        if aid not in drafts:
            drafts[aid] = AgentDraft(aid, surface, c.domain, directory.for_device(c.host, c.user), c.host, [])
        drafts[aid].members.append(c)
    # link identity components
    for ic in identity:
        owner = directory.for_email(ic.owner_email)
        best, best_score = None, 0
        app_toks = _tokens(ic.name)
        for d in drafts.values():
            if d.owner.id != owner.id:
                continue
            score = 0
            names = set()
            for m in d.members:
                names |= _tokens(m.name) | _tokens(m.facts.get("package")) | _tokens(m.facts.get("path", "").rsplit("/", 1)[-1])
            score += 2 * len(app_toks & names)
            shared = set(ic.reach) & {s for m in d.members for s in m.reach}
            score += 2 * len(shared)
            if ic.kind == "llm_project" and set(ic.llm) & {p for m in d.members for p in m.llm}:
                score += 3
            if score > best_score:
                best, best_score = d, score
        if best and best_score >= 2:
            best.linked.append(ic)
        else:
            aid = _stable_id("saas", ic.key)
            drafts[aid] = AgentDraft(aid, "saas", "business", owner, ic.host, [ic])
    return list(drafts.values())


def _write_first(via: str) -> tuple[int, str]:
    """Order 'via' strings so the ones that justify write access come first."""
    v = via.lower()
    if "tools/list" in v or "calls " in v or re.search(r"(readwrite|send|modify|unrestricted)", v):
        return (0, via)
    if "known package" in v:
        return (1, via)
    return (2, via)


def risk_for(d: AgentDraft, reach: dict[str, Reach], llm: dict[str, set[str]], policy: Policy) -> dict:
    flags = []

    def flag(code, sev, text, ids):
        flags.append({"code": code, "severity": sev, "text": text, "evidence_ids": sorted(ids)[:8]})

    for sysname, r in reach.items():
        if "w" in r.access and sysname in policy.systems_of_record:
            sev = "high" if sysname in ("Salesforce", "HubSpot", "Postgres") else "medium"
            how = "; ".join(sorted(r.via, key=_write_first)[:2])
            flag("write_system_of_record", sev, f"Can write to {sysname} (system of record) via {how}", r.evidence)
    for m in d.members + d.linked:
        if m.kind == "mcp_server":
            secret_env = [k for k in m.facts.get("env_keys", []) if SENSITIVE_ENV.search(k)]
            if secret_env or "Authorization" in m.facts.get("header_keys", []):
                flag("plaintext_credential", "low",
                     f"{m.name}: credential stored in {m.client} config ({', '.join(secret_env or ['Authorization header'])})",
                     [m.of("mcp_config")[-1]["id"]])
            if "--access-mode=unrestricted" in m.facts.get("args", []):
                flag("unrestricted_db", "high", f"{m.name}: unrestricted SQL (read/write/DDL) on a database",
                     [m.of("mcp_config")[-1]["id"]])
        if m.kind == "script":
            secret_env = [n for n in m.facts.get("env_names", []) if SENSITIVE_ENV.search(n)]
            env_sig = [s["id"] for s in m.of("code_import") if s["raw"].get("env_file")]
            if secret_env and env_sig:
                flag("plaintext_credential", "low", f"{m.name}: credentials in a local .env file ({', '.join(secret_env[:4])})",
                     env_sig)
        if m.kind == "oauth_app":
            for s in m.signals:
                r = s["raw"]
                if r.get("event") == "app_role_assignment" and r.get("permission_type") == "application":
                    flag("tenant_wide_app_permission", "high",
                         f"App '{r.get('app_name')}' has application permission {', '.join(r.get('scopes') or [])} "
                         f"(every mailbox in the tenant)", [s["id"]])
                if r.get("event") == "consent" and r.get("consent_type") == "user":
                    company = [x for x in m.reach if x in policy.company_data_systems]
                    if company:
                        personal = r.get("client_type") == "NATIVE_DESKTOP" or "personal" in (r.get("app_name") or "").lower()
                        flag("personal_oauth", "high" if personal else "medium",
                             f"{'Personal' if personal else 'User'} OAuth grant to '{r.get('app_name')}' on company data "
                             f"({', '.join(company)}: {' '.join(r.get('scopes') or [])[:80]})", [s["id"]])
        if m.kind == "llm_project":
            for s in m.signals:
                r = s["raw"]
                if r.get("event") == "api_key.created" and r.get("owner_type") == "service_account":
                    flag("service_account_key", "medium",
                         f"New {r.get('provider')} API key on service account '{r.get('owner_name')}' created by an individual",
                         [s["id"]])
    for prov, ids in llm.items():
        if prov not in policy.approved_llm_providers:
            flag("unapproved_llm", "medium", f"Uses {prov} directly (not an approved LLM provider)", ids)
    level = max((f["severity"] for f in flags), key=lambda s: SEVERITY[s], default="none")
    flags.sort(key=lambda f: -SEVERITY[f["severity"]])
    return {"level": level, "flags": flags}


def classify_rules(d: AgentDraft, policy: Policy) -> tuple[str, str]:
    names = {m.name for m in d.members + d.linked}
    if names & policy.sanctioned_apps:
        return "ok", "Sanctioned app per policy"
    if d.domain == "engineering":
        return "govern_in_place", "Coding agent: inventory and risk only, no migration"
    if d.surface == "saas":
        return "govern_in_place", "Third-party SaaS app: govern access, can't host it in Langdock"
    return "replace", "Business automation on engineering-free systems: candidate to host in Langdock"


def evidence_summary(d: AgentDraft, agent: dict) -> dict:
    """Stable, compact, redacted bundle for the LLM. No timestamps/counts that change per run."""
    items: list[dict] = []

    def add(kind: str, sig_ids: list[str], **data):
        items.append({"eid": f"E{len(items) + 1}", "kind": kind, "signal_ids": sig_ids, **data})

    for m in d.members:
        if m.kind == "mcp_server":
            cfg = m.of("mcp_config")[-1]
            add("mcp_server_config", [cfg["id"]], client=m.client, server_name=m.name, transport=m.facts["transport"],
                package_or_url=m.facts["package"], known_package=m.facts.get("package_label"),
                args=m.facts["args"], env_var_names=m.facts["env_keys"], project=m.facts.get("project"))
            pr = m.of("mcp_probe")
            if pr:
                p = pr[-1]["raw"]
                if p.get("status") == "ok":
                    add("mcp_tools", [pr[-1]["id"]], server_name=m.name, server_info=p.get("server_info"),
                        instructions=p.get("instructions"),
                        tools=[{"name": t["name"], "description": t.get("description")} for t in p.get("tools") or []])
                else:
                    add("mcp_probe", [pr[-1]["id"]], server_name=m.name, status=p.get("status"),
                        detail=p.get("error") or p.get("reason"))
        if m.kind == "script":
            for s in m.of("code_import"):
                r = s["raw"]
                add("code", [s["id"]], file=r.get("relative_path"), sdks=r.get("sdks"), business_sdks=r.get("systems"),
                    env_var_names=r.get("env_names"), docstring=r.get("docstring"), prompts=r.get("prompts"),
                    method_calls=r.get("calls"))
        chain = next((s["raw"].get("parent_chain") for s in m.of("process")), None)
        if chain:
            add("process_parent_chain", [m.of("process")[0]["id"]], server_or_script=m.name,
                chain=[h.get("name") for h in chain][:4])
    for m in d.members + d.linked:
        if m.kind in ("oauth_app", "llm_project"):
            for s in m.signals:
                r = s["raw"]
                if r.get("event") in ("consent", "app_role_assignment", "app_registered", "api_key.created", "service_account.created"):
                    add("identity_event", [s["id"]], provider=r.get("provider"), event=r.get("event"),
                        app=r.get("app_name") or r.get("project_name"), scopes=r.get("scopes"),
                        permission_type=r.get("permission_type"), consent_type=r.get("consent_type"),
                        owner_type=r.get("owner_type"))
    for sysname, r in agent["_reach"].items():
        add("data_reach", sorted(r.evidence)[:5], system=sysname, access=r.access, via=sorted(r.via))
    for prov, ids in agent["_llm"].items():
        add("llm_provider", sorted(ids)[:3], provider=prov)
    for t in agent["triggers"]:
        add("trigger", t["evidence_ids"][:3], type=t["type"], description=re.sub(r" \(\d+ runs seen\)", "", t["description"]))
    return {
        "surface": d.surface, "domain": d.domain,
        "owner": {"name": d.owner.name, "department": d.owner.department, "title": d.owner.title},
        "evidence": items,
    }


def build_agents(signals: Iterable[dict], directory: Directory, policy: Policy) -> list[dict]:
    comps = build_components(signals)
    for c in comps:
        analyse(c)
    agents = []
    for d in group_agents(comps, directory):
        reach: dict[str, Reach] = defaultdict(Reach)
        llm: dict[str, set[str]] = defaultdict(set)
        for m in d.members + d.linked:
            for sysname, r in m.reach.items():
                tgt = reach[sysname]
                tgt.access = _merge_access(tgt.access if (tgt.evidence or tgt.via) else None, r.access)
                tgt.evidence |= r.evidence
                tgt.via |= r.via
            for p, ids in m.llm.items():
                llm[p] |= ids
        all_sigs = [s for m in d.members + d.linked for s in m.signals]
        tss = [s["ts"] for s in all_sigs]
        classification, why = classify_rules(d, policy)
        names = [m.name for m in d.members]
        label = CLIENT_LABEL.get(d.surface, d.surface)
        if d.surface == "saas":
            name = names[0]
        elif d.surface == "script":
            name = names[0]
        else:
            name = f"{label}: {', '.join(names)}"
        reach_txt = ", ".join(f"{k} ({v.access})" for k, v in sorted(reach.items())) or "unknown systems"
        agent = {
            "id": d.id,
            "name": name,
            "purpose": f"{'Coding' if d.domain == 'engineering' else 'Business'} agent in {label} reaching {reach_txt}",
            "owner": d.owner.to_dict(),
            "surface": d.surface,
            "domain": d.domain,
            "host": d.host,
            "members": [{"kind": m.kind, "name": m.name, "fingerprint": m.fingerprint, "client": m.client,
                         **{k: m.facts.get(k) for k in ("package", "package_label", "transport", "path", "config_path", "url")
                            if m.facts.get(k)}} for m in d.members],
            "linked_identity": [{"kind": m.kind, "name": m.name, "provider": m.facts.get("provider"),
                                 "fingerprint": m.fingerprint} for m in d.linked],
            "tools": [t for m in d.members for t in m.tools],
            "data_reach": [{"system": k, "access": v.access, "via": sorted(v.via), "evidence_ids": sorted(v.evidence)[:10],
                            "source": "evidence"} for k, v in sorted(reach.items())],
            "llm_providers": sorted(llm),
            "classification": classification,
            "classification_source": "rules",
            "rationale": {"classification": why},
            "confidence": None,
            "status": "provisional",
            "first_seen": min(tss) if tss else None,
            "last_seen": max(tss) if tss else None,
            "evidence_signal_ids": [s["id"] for s in all_sigs],
            "_reach": reach,
            "_llm": llm,
        }
        agent["triggers"] = triggers_for(d.members, d.surface)
        agent["risk"] = risk_for(d, reach, llm, policy)
        summary = evidence_summary(d, agent)
        agent["_evidence"] = summary
        agent["evidence_hash"] = hashlib.sha256(json.dumps(summary, sort_keys=True, default=str).encode()).hexdigest()[:16]
        agents.append(agent)
    agents.sort(key=lambda a: a["first_seen"] or "")
    return agents


def public(agent: dict) -> dict:
    return {k: v for k, v in agent.items() if not k.startswith("_")}
