from pathlib import Path

from backend.enrich import CARD_SCHEMA, apply_card
from backend.org import directory, policy
from backend.resolve import build_agents, cron_text, recurrence
from common.schema import SignalIn
from scripts.seed import load
from sensor.parsers import parse_data
from sensor.paths import ConfigLocation

DEMO_TOOLS = [  # what scripts/demo_mcp_server.py returns from tools/list
    ("search_customer_emails", "Search the shared Gmail support inbox for customer emails matching a Gmail query."),
    ("get_email_thread", "Fetch every message in a Gmail thread (sender, date, body text)."),
    ("summarize_thread", "Summarize a customer email thread: ask, sentiment, urgency, next step."),
    ("lookup_account", "Look up the sender's company, plan and owner in HubSpot CRM."),
    ("draft_reply", "Create a Gmail draft reply to the thread (does not send)."),
    ("post_digest", "Post the daily inbox digest to a Slack channel."),
]


def fixture_agents():
    return build_agents([s for _, s in load()], directory(), policy())


def by_name(agents, owner, needle):
    return next(a for a in agents if a["owner"]["name"] == owner and needle in a["name"])


def test_fixture_org_resolves_to_expected_agents():
    agents = fixture_agents()
    assert len(agents) == 10
    maya = by_name(agents, "Maya Patel", "inbox-summaries")
    assert maya["surface"] == "claude_desktop" and maya["classification"] == "replace"
    assert {r["system"] for r in maya["data_reach"]} == {"Gmail"}
    assert [l["name"] for l in maya["linked_identity"]] == ["gmail-mcp (maya personal project)"]

    jonas = by_name(agents, "Jonas Weber", "customer-email-digest")
    assert {m["name"] for m in jonas["members"]} == {"customer-email-digest", "hubspot"}  # one agent, not two
    assert {r["system"] for r in jonas["data_reach"]} == {"Microsoft 365", "HubSpot"}

    priya = by_name(agents, "Priya Raman", "summarize_customer_emails")
    assert {l["name"] for l in priya["linked_identity"]} == {"AR Inbox Summarizer", "Finance Automation"}
    assert priya["triggers"][0]["cron"] == "30 7 * * 1-5"
    assert priya["llm_providers"] == ["OpenAI"]

    lena = by_name(agents, "Lena Hoffmann", "weekly_pipeline_report")
    slack = next(r for r in lena["data_reach"] if r["system"] == "Slack")
    assert "w" in slack["access"]  # chat_postMessage() in the code

    alex = [a for a in agents if a["owner"]["name"] == "Alex Kim"]
    assert {a["surface"] for a in alex} == {"cursor", "claude_code"}
    assert all(a["classification"] == "govern_in_place" and a["domain"] == "engineering" for a in alex)

    saas = {a["name"]: a for a in agents if a["surface"] == "saas"}
    assert set(saas) == {"Langdock", "ChatGPT", "Fireflies.ai Notetaker"}
    assert saas["Langdock"]["classification"] == "ok"


def test_risk_flags_from_evidence():
    agents = fixture_agents()
    codes = lambda a: {f["code"] for f in a["risk"]["flags"]}  # noqa: E731
    assert {"tenant_wide_app_permission", "unapproved_llm", "service_account_key"} <= codes(
        by_name(agents, "Priya Raman", "summarize"))
    assert {"unrestricted_db", "write_system_of_record"} <= codes(by_name(agents, "Alex Kim", "Cursor"))
    assert "personal_oauth" in codes(by_name(agents, "Maya Patel", "inbox-summaries"))
    tom = by_name(agents, "Tom Becker", "pipeline-review")
    assert any(f["code"] == "write_system_of_record" and "Salesforce" in f["text"] for f in tom["risk"]["flags"])
    assert by_name(agents, "Lena Hoffmann", "weekly")["risk"]["level"] in ("none", "low")  # Anthropic is approved
    for a in agents:  # every flag cites evidence
        assert all(f["evidence_ids"] for f in a["risk"]["flags"])


def _presenter_signals(with_github: bool) -> list[dict]:
    cfg = {"mcpServers": {"inbox-digest": {"url": "http://127.0.0.1:8931/mcp", "headers": {"Authorization": "x"}}}}
    if with_github:
        cfg["mcpServers"]["github"] = {"url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "x"}}
    loc = ConfigLocation("cursor", Path("/Users/me/.cursor/mcp.json"), "mcpServers", "user")
    out = []
    for s in parse_data(cfg, loc):
        out.append(SignalIn(source="sensor:mcp_config:cursor", host="me-mbp", user="me", kind="mcp_config",
                            raw={**s.public(), "change": "added", "server_fingerprint": s.fingerprint},
                            fingerprint=s.fingerprint).model_dump())
        if s.name == "inbox-digest":
            tools = [{"name": n, "description": d} for n, d in DEMO_TOOLS]
            out.append(SignalIn(source="sensor:mcp_probe", host="me-mbp", user="me", kind="mcp_probe",
                                raw={"server_name": s.name, "status": "ok", "tools": tools,
                                     "server_info": {"name": "inbox-digest"}, "server_fingerprint": s.fingerprint},
                                fingerprint=s.fingerprint).model_dump())
    return out


def test_live_demo_agent_is_split_from_coding_servers_in_same_cursor_config():
    agents = build_agents(_presenter_signals(with_github=True), directory(), policy())
    assert len(agents) == 2
    coding = next(a for a in agents if a["domain"] == "engineering")
    biz = next(a for a in agents if a["domain"] == "business")
    assert [m["name"] for m in coding["members"]] == ["github"]
    assert [m["name"] for m in biz["members"]] == ["inbox-digest"]
    assert biz["classification"] == "replace" and coding["classification"] == "govern_in_place"
    reach = {r["system"]: r["access"] for r in biz["data_reach"]}
    assert reach == {"Gmail": "rw", "HubSpot": "r", "Slack": "w"}
    assert len(biz["tools"]) == 6
    assert biz["owner"]["in_directory"] is False  # presenter laptop, not in the simulated directory


def test_agent_id_is_stable_when_a_server_is_added():
    alone = build_agents(_presenter_signals(with_github=False), directory(), policy())
    both = build_agents(_presenter_signals(with_github=True), directory(), policy())
    assert alone[0]["id"] in {a["id"] for a in both}


def test_evidence_hash_ignores_extra_runs_of_the_same_pattern():
    sigs = [s for _, s in load()]
    procs = [i for i, s in enumerate(sigs) if s["host"] == "mpatel-mbp" and s["kind"] == "process"]
    fewer = [s for i, s in enumerate(sigs) if i not in procs[-6:]]  # drop the last 3 runs (start+exit)
    h = lambda ss: next(a for a in build_agents(ss, directory(), policy())  # noqa: E731
                        if a["owner"]["name"] == "Maya Patel")["evidence_hash"]
    assert h(sigs) == h(fewer)


def test_cron_text_and_recurrence():
    assert cron_text("30 7 * * 1-5") == "Mon–Fri 07:30"
    assert cron_text("0 8 * * 1") == "Mon 08:00"
    from datetime import datetime, timedelta
    base = datetime(2026, 11, 2, 9, 7)  # Monday
    starts = [base + timedelta(days=d, minutes=m) for d, m in [(0, 0), (3, 5), (7, 2), (10, 9), (14, 1), (17, 4)]]
    rec = recurrence(starts)
    assert rec["pattern"] == "Mon & Thu" and rec["time"] == "~09:15"


def _walk(schema):
    yield schema
    for v in (schema.get("properties") or {}).values():
        yield from _walk(v)
    if isinstance(schema.get("items"), dict):
        yield from _walk(schema["items"])


def test_card_schema_is_strict():
    for node in _walk(CARD_SCHEMA):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])


def test_apply_card_merges_without_dropping_evidence():
    agents = fixture_agents()
    priya = by_name(agents, "Priya Raman", "summarize")
    eids = {e["kind"]: e["eid"] for e in priya["_evidence"]["evidence"]}
    card = {  # shape-only input for the merge logic (not a model output)
        "name": "AR email summarizer", "purpose": "p", "purpose_rationale": "r", "purpose_evidence": [eids["code"]],
        "capabilities": ["read inbox"], "classification": "replace", "classification_rationale": "c",
        "instructions_summary": "i", "confidence": 0.55, "confidence_rationale": "x",
        "data_reach": [{"system": "Microsoft 365", "access": "rw", "rationale": "saves xlsx to OneDrive",
                        "evidence": [eids["code"]]}],
        "triggers": [{"type": "schedule", "description": "cron", "cron": "30 7 * * 1-5", "evidence": []}],
    }
    before = {r["system"] for r in priya["data_reach"]}
    out = apply_card(priya, card, {"enabled": True})
    assert out["status"] == "needs_review"  # confidence < 0.6
    m365 = next(r for r in out["data_reach"] if r["system"] == "Microsoft 365")
    assert m365["access"] == "rw" and m365["source"] == "evidence+llm"
    assert {r["system"] for r in out["data_reach"]} == before
    assert len([t for t in out["triggers"] if t["type"] == "schedule"]) == 1  # no duplicate cron
    assert out["rationale"]["purpose_evidence_ids"]
