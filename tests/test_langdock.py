import asyncio
import json

import httpx

from backend.langdock import assemble, cron_to_schedule, execute, integrations_for, to_markdown


def test_cron_to_scheduled_task_fields():
    assert cron_to_schedule("30 7 * * 1-5") == {"frequency": "WEEKDAYS", "timeOfDay": "07:30"}
    assert cron_to_schedule("0 8 * * 1") == {"frequency": "WEEKLY", "timeOfDay": "08:00", "dayOfWeek": 1}
    assert cron_to_schedule("0 8 * * 7")["dayOfWeek"] == 0  # cron Sunday=7 -> Langdock 0
    assert cron_to_schedule("15 9 * * 1,4") == {"frequency": "SELECTED_WEEKDAYS", "timeOfDay": "09:15", "daysOfWeek": [1, 4]}
    assert cron_to_schedule("0 6 * * *") == {"frequency": "DAILY", "timeOfDay": "06:00"}
    assert cron_to_schedule("0 6 1 * *") == {"frequency": "MONTHLY", "timeOfDay": "06:00", "dayOfMonth": 1}
    assert cron_to_schedule("*/5 * * * *")["frequency"] == "MANUAL"


def _agent(aid, owner, dept, systems, caps, triggers=()):
    return {"id": aid, "name": f"{owner}'s agent", "surface": "script", "purpose": " ".join(caps), "capabilities": caps,
            "evidence_hash": aid, "owner": {"id": owner, "name": owner, "department": dept},
            "data_reach": [{"system": s, "access": a} for s, a in systems], "triggers": list(triggers),
            "members": [{"kind": "script", "name": "x.py", "path": "/u/x.py"}], "linked_identity": [],
            "risk": {"flags": []}}


MEMBERS = [
    _agent("a1", "Maya", "Support", [("Gmail", "rw")], ["read inbox", "summarize threads"]),
    _agent("a2", "Priya", "Finance", [("Microsoft 365", "r")], ["read unread emails", "write digest to SharePoint"],
           [{"type": "schedule", "cron": "30 7 * * 1-5", "description": "cron"}]),
]
CLUSTER = {"id": "cl_a1", "label": "Customer email summarizer", "base_purpose": "Summarize customer emails.",
           "shared_capabilities": ["read inbox"], "status": "confirmed",
           "variants": [{"agent_id": "a1", "diff": "baseline"}, {"agent_id": "a2", "diff": "AR mailbox, SharePoint"}]}
WRITTEN = {"name": "Customer Email Summarizer", "emoji": "📧", "description": "d", "base_instructions": "Base.",
           "variant_sections": [{"member": "M2", "title": "Finance AR inbox", "instructions": "Use ar@."}],
           "conversation_starters": ["Summarize today's emails"], "knowledge_suggestions": []}


def test_integrations_map_to_native_langdock_integrations():
    names = {i["langdock_integration"]: i for i in integrations_for(MEMBERS)}
    assert set(names) == {"Gmail", "Microsoft Outlook", "Microsoft SharePoint"}
    assert names["Gmail"]["access"] == "read + write" and names["Microsoft Outlook"]["used_by"] == ["Priya"]


def test_assembled_requests_follow_documented_api():
    d = assemble(CLUSTER, MEMBERS, WRITTEN, {"M1": "a1", "M2": "a2"})
    create, publish, sched = d["api_requests"]
    assert create["url"].endswith("/agent/v1/create") and publish["url"].endswith("/agent/v1/publish")
    assert sched["url"].endswith("/automations/v1")
    body = create["body"]
    assert set(body) <= {"name", "description", "emoji", "icon", "instruction", "inputType", "model", "creativity",
                         "conversationStarters", "actions", "inputFields", "attachments", "knowledgeFolderIds",
                         "skillIds", "subAgents", "workflowIds", "webSearch", "imageGeneration", "extendedThinking", "access"}
    assert len(body["name"]) <= 80 and len(body["description"]) <= 800 and len(body["instruction"]) <= 50000
    assert "### Finance AR inbox (Finance)" in body["instruction"]
    assert sched["body"]["frequency"] == "WEEKDAYS" and sched["body"]["timeOfDay"] == "07:30"
    assert sched["body"]["assistantId"] == "{{agent.id}}" and "Use ar@." in sched["body"]["prompt"]
    assert "WEEKDAYS 07:30" in to_markdown(d)


def test_execute_substitutes_agent_id_and_stops_on_error():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, json.loads(req.content), req.headers["authorization"]))
        if req.url.path == "/agent/v1/create":
            return httpx.Response(201, json={"status": "success", "agent": {"id": "ld-123"}})
        if req.url.path == "/agent/v1/publish":
            return httpx.Response(200, json={"status": "success", "version": 1})
        return httpx.Response(400, json={"error": "AUTOMATION_LIMIT_REACHED"})

    d = assemble(CLUSTER, MEMBERS, WRITTEN, {"M1": "a1", "M2": "a2"})
    log = asyncio.run(execute(d["api_requests"] + d["api_requests"][2:], "k", httpx.MockTransport(handler)))
    assert [e["status"] for e in log] == [201, 200, 400]  # stopped after the first failure
    assert seen[1][1]["agentId"] == "ld-123" and seen[2][1]["assistantId"] == "ld-123"
    assert all(h == "Bearer k" for _, _, h in seen)
