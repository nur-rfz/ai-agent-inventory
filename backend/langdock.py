"""Phase 5: turn a confirmed duplicate cluster into one Langdock agent (+ schedules).

What Langdock's public API supports (docs.langdock.com, read Nov 2026):
  POST /agent/v1/create        name<=80, description<=800, emoji, instruction<=50000, inputType,
                               model, creativity, conversationStarters, actions[{actionId}],
                               knowledgeFolderIds, workflowIds, access   (scope: Agent API)
  POST /agent/v1/publish       {agentId, description<=100}
  POST /automations/v1         scheduled task: name, prompt, frequency (DAILY | WEEKDAYS | WEEKLY |
                               MONTHLY | SELECTED_WEEKDAYS | MANUAL), timeOfDay HH:mm, dayOfWeek,
                               daysOfWeek, dayOfMonth, timezone, assistantId  (scope: AUTOMATION_API)
Not possible via the API (so they become admin steps in the spec):
  - connecting native integrations / OAuth ("Pre-selected OAuth connections are not supported
    via the API"), and attaching their actions needs actionIds of enabled integrations;
  - adding MCP servers (URL + auth + "List Server Features" in the UI; private URLs blocked).

Draft = deterministic mapping (integrations, schedules, decommission, exact API requests)
      + LLM-written instructions (base + one section per variant), cached per cluster.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any

import httpx

from common.redact import scrub
from common.schema import now_iso

from . import llm

log = logging.getLogger("langdock")

BASE_URL = os.environ.get("LANGDOCK_BASE_URL", "https://api.langdock.com").rstrip("/")
TIMEZONE = os.environ.get("LANGDOCK_TIMEZONE", "Europe/Berlin")
PROMPT_VERSION = "d1"

# business system -> Langdock native integration (Integration Directory)
NATIVE = {
    "Gmail": "Gmail", "Microsoft 365": "Microsoft Outlook", "HubSpot": "HubSpot", "Salesforce": "Salesforce",
    "Slack": "Slack", "Google Drive": "Google Drive", "Jira": "Jira", "Confluence": "Confluence", "Notion": "Notion",
    "Zendesk": "Zendesk", "GitHub": "GitHub",
}
FILE_HINT = re.compile(r"(sharepoint|onedrive|xlsx|excel|spreadsheet|file|document)", re.I)


# --------------------------------------------------------------------------------------
# deterministic mapping
# --------------------------------------------------------------------------------------

def cron_to_schedule(expr: str | None) -> dict:
    """Map a 5-field cron expression to Langdock Scheduled Task fields."""
    parts = (expr or "").split()
    if len(parts) != 5 or not parts[0].isdigit() or not parts[1].isdigit():
        return {"frequency": "MANUAL", "note": f"cron '{expr}' has no direct equivalent; run manually or as a workflow"}
    mi, hr, dom, mon, dow = parts
    t = f"{int(hr):02d}:{int(mi):02d}"
    if mon != "*":
        return {"frequency": "MANUAL", "note": f"month-restricted cron '{expr}' not supported by Scheduled Tasks"}
    if dom != "*" and dow == "*" and dom.isdigit():
        return {"frequency": "MONTHLY", "timeOfDay": t, "dayOfMonth": int(dom)}
    if dow == "*":
        return {"frequency": "DAILY", "timeOfDay": t}
    if dow in ("1-5", "MON-FRI", "mon-fri"):
        return {"frequency": "WEEKDAYS", "timeOfDay": t}
    days: list[int] = []
    for part in dow.split(","):
        if "-" in part:
            a, b = part.split("-")
            days += list(range(int(a), int(b) + 1))
        elif part.isdigit():
            days.append(int(part))
    days = sorted({d % 7 for d in days})  # cron 7 = Sunday = Langdock 0
    if len(days) == 1:
        return {"frequency": "WEEKLY", "timeOfDay": t, "dayOfWeek": days[0]}
    if days:
        return {"frequency": "SELECTED_WEEKDAYS", "timeOfDay": t, "daysOfWeek": days}
    return {"frequency": "MANUAL", "note": f"cron '{expr}' not understood"}


def integrations_for(members: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for a in members:
        text = " ".join((a.get("capabilities") or []) + [a.get("purpose") or ""])
        for r in a["data_reach"]:
            sysname = r["system"]
            names = [NATIVE.get(sysname)]
            if sysname == "Microsoft 365" and FILE_HINT.search(text):
                names.append("Microsoft SharePoint")
            for name in filter(None, names):
                e = out.setdefault(name, {"langdock_integration": name, "system": sysname, "access": "", "used_by": [],
                                          "native": True})
                e["access"] = "".join(sorted(set(e["access"]) | set(r["access"])))
                if a["owner"]["name"] not in e["used_by"]:
                    e["used_by"].append(a["owner"]["name"])
    for e in out.values():
        acc = set(e["access"])
        e["access"] = "read + write" if acc >= {"r", "w"} else ("write" if acc == {"w"} else "read")
        e["setup"] = (f"Integration Directory → enable {e['langdock_integration']}, users connect their own account "
                      f"(OAuth can't be pre-selected via API), then add its actions to the agent")
    return sorted(out.values(), key=lambda e: -len(e["used_by"]))


def schedules_for(members: list[dict]) -> list[dict]:
    out = []
    for a in members:
        for t in a["triggers"]:
            if t["type"] == "schedule" and t.get("cron"):
                sched = cron_to_schedule(t["cron"])
                out.append({"agent_id": a["id"], "owner": a["owner"]["name"], "department": a["owner"].get("department"),
                            "cron": t["cron"], "timezone": TIMEZONE, **sched,
                            "source": t.get("source", "evidence")})
    return out


def decommission_for(members: list[dict]) -> list[dict]:
    steps = []
    for a in members:
        owner = a["owner"]["name"]
        for m in a["members"]:
            if m["kind"] == "mcp_server":
                steps.append({"owner": owner, "step": f"Remove MCP server '{m['name']}' from {m.get('client')} config "
                                                      f"({m.get('config_path') or 'client settings'})"})
            elif m["kind"] == "script":
                steps.append({"owner": owner, "step": f"Disable cron/launchd job and archive {m.get('path') or m['name']}"})
        for li in a.get("linked_identity", []):
            if li["kind"] == "oauth_app":
                steps.append({"owner": owner, "step": f"Revoke {li['provider']} grant for app '{li['name']}'"})
            elif li["kind"] == "llm_project":
                steps.append({"owner": owner, "step": f"Revoke API keys in {li['provider']} project '{li['name']}'"})
        for f in a["risk"]["flags"]:
            if f["severity"] == "high":
                steps.append({"owner": owner, "step": f"Resolved by migration: {f['text']}"})
    return steps


# --------------------------------------------------------------------------------------
# LLM-written instructions
# --------------------------------------------------------------------------------------

_STR = {"type": "string"}
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "emoji", "description", "base_instructions", "variant_sections", "conversation_starters",
                 "knowledge_suggestions"],
    "properties": {
        "name": _STR, "emoji": _STR, "description": _STR, "base_instructions": _STR,
        "variant_sections": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["member", "title", "instructions"],
            "properties": {"member": _STR, "title": _STR, "instructions": _STR}}},
        "conversation_starters": {"type": "array", "items": _STR},
        "knowledge_suggestions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "contents", "reason"],
            "properties": {"name": _STR, "contents": _STR, "reason": _STR}}},
    },
}

DRAFT_PROMPT = """You write the configuration for ONE shared agent on Langdock that replaces several agents employees built on their own for the same job.

You get the cluster (shared job, base purpose, shared capabilities), each member's variant, their visible prompts or instruction summaries, systems and schedules, and the Langdock integrations the agent will use.

Return:
- name: <= 60 chars, the shared job ("Customer Email Summarizer").
- emoji: one emoji.
- description: <= 300 chars, what it does and for whom.
- base_instructions: the system prompt for the shared agent, in Markdown, 150-400 words: role, the step-by-step workflow, output format, rules (cite sources, don't send email or write to systems without the user confirming, keep customer data inside the company). Reuse the best wording from members' visible prompts. Refer to integrations by their Langdock names. No person names.
- variant_sections: one per member variant that genuinely differs (skip "baseline"): member = the member ref (M1, M2, ...), title = team/use case ("Finance AR inbox"), instructions = 2-5 sentences that apply only when that team uses the agent (different mailbox, extra CRM step, output destination, tone).
- conversation_starters: 3-5 short prompts users will click.
- knowledge_suggestions: 0-3 knowledge folders worth attaching (e.g. reply templates, dispute policy), with what goes in them and why. Only if the evidence suggests it.
Keep it concrete and grounded in the evidence. Do not invent systems the members don't use."""


def _draft_payload(cluster: dict, members: list[dict], raw_agents: dict[str, dict], integrations: list[dict],
                   schedules: list[dict]) -> tuple[str, dict[str, str]]:
    refs = {f"M{i + 1}": a["id"] for i, a in enumerate(members)}
    variants = {v["agent_id"]: v["diff"] for v in cluster["variants"]}
    items = []
    for ref, a in zip(refs, members):
        ev = (raw_agents.get(a["id"]) or {}).get("_evidence", {}).get("evidence", [])
        prompts = [p for e in ev if e["kind"] == "code" for p in (e.get("prompts") or [])]
        instructions = [e.get("instructions") for e in ev if e["kind"] == "mcp_tools" and e.get("instructions")]
        items.append({"ref": ref, "team": a["owner"].get("department"), "role": a["owner"].get("title"),
                      "variant": variants.get(a["id"]), "purpose": a["purpose"],
                      "instructions_summary": a.get("instructions_summary"), "visible_prompts": prompts[:2],
                      "server_instructions": instructions[:1], "capabilities": a.get("capabilities"),
                      "systems": [f"{r['system']} ({r['access']})" for r in a["data_reach"]],
                      "triggers": [t["description"] for t in a["triggers"]]})
    payload = {"cluster": {k: cluster.get(k) for k in ("label", "base_purpose", "shared_capabilities")},
               "members": items,
               "langdock_integrations": [{"name": i["langdock_integration"], "access": i["access"]} for i in integrations],
               "schedules": [{"team": s["department"], "frequency": s["frequency"], "time": s.get("timeOfDay")}
                             for s in schedules]}
    return "Cluster to consolidate:\n" + json.dumps(scrub(payload), indent=1), refs


def draft_key(cluster: dict, members: list[dict]) -> str:
    blob = json.dumps([cluster.get("label"), cluster.get("base_purpose"),
                       sorted((a["id"], a["evidence_hash"]) for a in members)], default=str)
    return hashlib.sha256(f"{blob}|{llm.PROVIDER}:{llm.MODEL}|{PROMPT_VERSION}".encode()).hexdigest()[:20]


def assemble(cluster: dict, members: list[dict], written: dict | None, refs: dict[str, str]) -> dict:
    """Full draft: deterministic mapping + LLM text (if any) + the exact API requests."""
    integrations = integrations_for(members)
    schedules = schedules_for(members)
    by_id = {a["id"]: a for a in members}
    if written:
        name = written["name"][:80]
        sections = [s for s in written["variant_sections"] if s["member"] in refs]
        instruction = written["base_instructions"].strip()
        if sections:
            instruction += "\n\n## Team variants\nApply the section that matches the user's team.\n"
            for s in sections:
                a = by_id[refs[s["member"]]]
                instruction += f"\n### {s['title']} ({a['owner'].get('department') or 'team'})\n{s['instructions'].strip()}\n"
        description, emoji = written["description"][:800], written["emoji"][:16]
        starters = [c[:255] for c in written["conversation_starters"]][:20]
        knowledge = written["knowledge_suggestions"]
    else:
        name = cluster["label"][:80]
        instruction = f"{cluster.get('base_purpose') or cluster['label']}\n\nCapabilities: " + \
                      ", ".join(cluster.get("shared_capabilities") or [])
        description, emoji, starters, knowledge = (cluster.get("base_purpose") or "")[:800], "🤖", [], []
    variants = [{"agent_id": v["agent_id"], "owner": by_id[v["agent_id"]]["owner"]["name"],
                 "department": by_id[v["agent_id"]]["owner"].get("department"), "diff": v["diff"],
                 "replaces": by_id[v["agent_id"]]["name"], "surface": by_id[v["agent_id"]]["surface"]}
                for v in cluster["variants"] if v["agent_id"] in by_id]
    agent_body = {"name": name, "description": description, "emoji": emoji, "instruction": instruction[:50000],
                  "inputType": "PROMPT", "creativity": 0.3, "conversationStarters": starters}
    requests = [{"step": "create_agent", "method": "POST", "url": f"{BASE_URL}/agent/v1/create", "body": agent_body},
                {"step": "publish_agent", "method": "POST", "url": f"{BASE_URL}/agent/v1/publish",
                 "body": {"agentId": "{{agent.id}}", "description": f"Consolidates {len(members)} shadow agents"[:100]}}]
    section_for = {refs[x["member"]]: x for x in (written or {}).get("variant_sections", []) if x["member"] in refs}
    for s in schedules:
        if s["frequency"] == "MANUAL":
            continue
        sec = section_for.get(s["agent_id"])
        team = s["department"] or s["owner"]
        prompt = (f"Scheduled run for {team}" + (f" ({sec['title']})" if sec else "") + ": process everything new since "
                  f"the last run and deliver the result." + (f"\n\n{sec['instructions'].strip()}" if sec else ""))
        body = {"name": f"{name} – {team}"[:200],
                "prompt": prompt[:120000],
                "frequency": s["frequency"], "timezone": s["timezone"], "assistantId": "{{agent.id}}",
                **{k: s[k] for k in ("timeOfDay", "dayOfWeek", "daysOfWeek", "dayOfMonth") if k in s}}
        requests.append({"step": f"schedule_{s['department'] or s['owner']}".lower().replace(" ", "_"),
                         "method": "POST", "url": f"{BASE_URL}/automations/v1", "body": body})
    manual = [f"{i['setup']} (needed by {', '.join(i['used_by'])}; {i['access']})" for i in integrations]
    manual += [f"Create knowledge folder '{k['name']}': {k['contents']}" for k in knowledge]
    manual += ["Share the agent with the members' teams (access.users / groups) once they're onboarded"]
    return {
        "cluster_id": cluster["id"], "status": "draft", "name": name, "emoji": emoji, "description": description,
        "instruction": instruction, "conversation_starters": starters, "integrations": integrations,
        "knowledge": knowledge, "schedules": schedules, "variants": variants,
        "decommission": decommission_for(members), "api_requests": requests, "manual_steps": manual,
        "people": len({a["owner"]["id"] for a in members}), "replaces_count": len(members),
        "written_by": "llm" if written else "rules",
    }


def to_markdown(d: dict) -> str:
    lines = [f"# {d['emoji']} {d['name']}", "", d["description"], "",
             f"Replaces **{d['replaces_count']} agents** built by **{d['people']} people**. Status: **{d['status']}**.", "",
             "## Replaces", ""]
    lines += [f"- {v['owner']} ({v['department'] or '-'}): *{v['replaces']}* on {v['surface']} — {v['diff']}" for v in d["variants"]]
    lines += ["", "## Instructions", "", d["instruction"], "", "## Integrations", ""]
    lines += [f"- **{i['langdock_integration']}** ({i['access']}) — used by {', '.join(i['used_by'])}" for i in d["integrations"]]
    if d["schedules"]:
        lines += ["", "## Schedules (Langdock Scheduled Tasks)", ""]
        lines += [f"- {s['department'] or s['owner']}: {s['frequency']} {s.get('timeOfDay', '')} {s['timezone']} (from cron `{s['cron']}`)"
                  for s in d["schedules"]]
    if d["conversation_starters"]:
        lines += ["", "## Conversation starters", ""] + [f"- {c}" for c in d["conversation_starters"]]
    lines += ["", "## Admin steps (not available via the API)", ""] + [f"1. {m}" for m in d["manual_steps"]]
    lines += ["", "## Decommission after rollout", ""] + [f"- [ ] {s['owner']}: {s['step']}" for s in d["decommission"]]
    lines += ["", "## API requests", "", "```json", json.dumps(d["api_requests"], indent=2), "```", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# creation (behind approval + LANGDOCK_API_KEY)
# --------------------------------------------------------------------------------------

def _subst(obj: Any, ctx: dict) -> Any:
    if isinstance(obj, str):
        return ctx.get(obj[2:-2], obj) if obj.startswith("{{") and obj.endswith("}}") else obj
    if isinstance(obj, dict):
        return {k: _subst(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_subst(v, ctx) for v in obj]
    return obj


async def execute(requests: list[dict], api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Send the planned requests in order; stop at the first failure. Returns a per-step log."""
    log_: list[dict] = []
    ctx: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=30, transport=transport,
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}) as c:
        for r in requests:
            body = _subst(r["body"], ctx)
            t0 = time.monotonic()
            resp = await c.request(r["method"], r["url"], json=body)
            entry = {"step": r["step"], "status": resp.status_code, "ms": int((time.monotonic() - t0) * 1000)}
            try:
                data = resp.json()
            except ValueError:
                data = {"text": resp.text[:300]}
            if resp.is_success:
                if r["step"] == "create_agent":
                    ctx["agent.id"] = data["agent"]["id"]
                    entry["agent_id"] = data["agent"]["id"]
                entry["response"] = scrub({k: data.get(k) for k in ("status", "message", "version", "id") if k in data})
                log_.append(entry)
            else:
                entry["error"] = scrub(data)
                log_.append(entry)
                break
    return log_


class DraftService:
    def __init__(self, on_change) -> None:
        self.llm = llm.StructuredLLM()
        self.on_change = on_change
        self.written: dict[str, dict] = {}
        self.inflight: set[str] = set()
        self.state: dict[str, dict] = {}  # cluster_id -> {status, approved_by, approved_at, created, log}
        self.disabled_reason = None if llm.credentials_configured() else llm.missing_credentials_hint()
        self.errors: dict[str, str] = {}
        from .db import DraftCache, DraftState, session

        with session() as s:
            self.written = {r.key: r.written for r in s.query(DraftCache).all()}
            self.state = {r.cluster_id: r.state for r in s.query(DraftState).all()}

    def draft(self, cluster: dict, members: list[dict], raw_agents: dict[str, dict]) -> dict:
        key = draft_key(cluster, members)
        integrations, schedules = integrations_for(members), schedules_for(members)
        _, refs = _draft_payload(cluster, members, raw_agents, integrations, schedules)
        d = assemble(cluster, members, self.written.get(key), refs)
        d["draft_key"] = key
        st = self.state.get(cluster["id"])
        if st and st.get("draft_key") == key:
            d.update({k: st[k] for k in ("status", "approved_by", "approved_at", "created_log", "langdock_agent_id")
                      if k in st})
        d["writing"] = key in self.inflight
        d["write_error"] = self.errors.get(key)
        if key not in self.written and cluster["status"] == "confirmed":
            self.request(key, cluster, members, raw_agents, integrations, schedules)
        return d

    def request(self, key, cluster, members, raw_agents, integrations, schedules) -> None:
        import asyncio

        if self.disabled_reason or key in self.inflight or key in self.errors:
            return
        self.inflight.add(key)
        asyncio.create_task(self._write(key, cluster, members, raw_agents, integrations, schedules))

    async def _write(self, key, cluster, members, raw_agents, integrations, schedules) -> None:
        from .db import DraftCache, session

        try:
            user, _ = _draft_payload(cluster, members, raw_agents, integrations, schedules)
            out, usage = await self.llm.structured(DRAFT_PROMPT, user, DRAFT_SCHEMA, "langdock_agent", max_tokens=4000)
            out = scrub(out)
            self.written[key] = out
            with session() as s:
                s.merge(DraftCache(key=key, written=out, created_at=now_iso()))
                s.commit()
        except Exception as e:
            log.warning("draft writing failed: %s", e)
            self.errors[key] = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            self.inflight.discard(key)
            self.on_change()

    def set_state(self, cluster_id: str, **kw) -> dict:
        from .db import DraftState, session

        st = {**self.state.get(cluster_id, {}), **kw}
        self.state[cluster_id] = st
        with session() as s:
            s.merge(DraftState(cluster_id=cluster_id, state=st, updated_at=now_iso()))
            s.commit()
        return st
