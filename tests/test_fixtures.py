import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common.redact import leaks
from scripts.seed import load, week_shift

ROOT = Path(__file__).resolve().parent.parent


def _events(now=None):
    return [s for _, s in load(now)]


def test_build_is_deterministic_and_committed_file_is_current(tmp_path):
    before = (ROOT / "fixtures" / "signals.jsonl").read_text()
    subprocess.run([sys.executable, "fixtures/build.py"], cwd=ROOT, check=True, capture_output=True)
    assert (ROOT / "fixtures" / "signals.jsonl").read_text() == before


def test_no_secret_shapes_anywhere_in_fixture_signals():
    evs = _events()
    assert all(leaks(s["raw"]) == [] for s in evs)
    blob = json.dumps(evs)
    for fake in ("fake-hubspot-private-app-token", "fake-slack-bot-token", "fake-github-pat", "fake-password"):
        assert fake not in blob


def test_scenario_coverage():
    evs = _events()
    configs = {(s["host"], s["raw"]["server_name"]) for s in evs if s["kind"] == "mcp_config"}
    scripts = {(s["host"], Path(s["raw"]["path"]).name) for s in evs if s["kind"] == "code_import" and s["raw"]["sdks"]}
    # 3x "summarize customer emails": Gmail MCP, Outlook MCP (+ CRM), Python + OpenAI + Graph
    assert ("mpatel-mbp", "inbox-summaries") in configs
    assert ("jweber-mbp", "outlook") in configs and ("jweber-mbp", "hubspot") in configs
    assert ("praman-mbp", "summarize_customer_emails.py") in scripts
    # 2x weekly pipeline report: Salesforce + Slack
    assert ("lhoffmann-mbp", "weekly_pipeline_report.py") in scripts
    assert ("tbecker-mbp", "pipeline-review") in configs and ("tbecker-mbp", "slack") in configs
    # engineer: Cursor + Claude Code with GitHub and Postgres
    assert {("akim-mbp", n) for n in ("github", "prod-db", "sentry", "linear")} <= configs
    # identity: OAuth grants (Entra + Google) and a new OpenAI key on a service account
    grants = {(s["raw"]["provider"], s["raw"].get("app_name")) for s in evs if s["kind"] == "oauth_grant"}
    assert ("google_workspace", "gmail-mcp (maya personal project)") in grants
    assert ("entra", "ms-365-mcp-server") in grants and ("entra", "Langdock") in grants
    key = [s for s in evs if s["kind"] == "api_key" and s["raw"]["event"] == "api_key.created"]
    assert key and key[0]["raw"]["owner_type"] == "service_account"


def test_signals_of_one_agent_share_a_fingerprint():
    kinds = defaultdict(set)
    for s in _events():
        kinds[(s["host"], s["fingerprint"])].add(s["kind"])
    script_kinds = [k for k in kinds.values() if "schedule" in k]
    assert len(script_kinds) == 2 and all({"code_import", "process", "net_conn"} <= k for k in script_kinds)
    assert sum({"mcp_config", "mcp_probe", "process", "net_conn"} <= k for k in kinds.values()) >= 5


def test_week_shift_keeps_weekday_and_lands_before_now():
    latest = datetime(2026, 11, 13, 18, 28, tzinfo=timezone.utc)  # Friday
    for now in (datetime(2026, 11, 15, tzinfo=timezone.utc), datetime(2027, 2, 3, 10, tzinfo=timezone.utc)):
        shifted = latest + week_shift(latest, now)
        assert shifted <= now and now - shifted < timedelta(weeks=1)
        assert shifted.weekday() == latest.weekday()


def test_seed_shifts_sensor_and_identity_consistently():
    now = datetime(2027, 1, 20, 12, tzinfo=timezone.utc)
    evs = _events(now)
    tss = [datetime.fromisoformat(s["ts"]) for s in evs]
    assert max(tss) <= now and now - max(tss) < timedelta(weeks=1)
    procs = [s for s in evs if s["kind"] == "process"]
    assert all(abs(datetime.fromtimestamp(s["raw"]["started_at"], timezone.utc) - datetime.fromisoformat(s["ts"]))
               < timedelta(seconds=1) for s in procs if s["raw"]["event"] == "started")
