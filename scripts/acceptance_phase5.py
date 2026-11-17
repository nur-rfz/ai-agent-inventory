"""Phase 5 acceptance: one click previews a single consolidated Langdock agent.

Live path: add inbox-digest in Cursor -> joins the email cluster -> the Langdock draft
(base + variants, integrations, schedules, exact API requests) includes it -> approve ->
create (real requests with LANGDOCK_API_KEY, otherwise a dry run that lists them).

  python scripts/acceptance_phase5.py
"""

from __future__ import annotations

import os
import sys
import time

from harness import Stack, wait_for

AGENT_FIELDS = {"name", "description", "emoji", "icon", "instruction", "inputType", "model", "creativity",
                "conversationStarters", "actions", "inputFields", "attachments", "knowledgeFolderIds", "skillIds",
                "subAgents", "workflowIds", "webSearch", "imageGeneration", "extendedThinking", "access"}
FREQ = {"MANUAL", "DAILY", "WEEKDAYS", "WEEKLY", "MONTHLY", "SELECTED_WEEKDAYS"}


def main() -> int:
    s = Stack(port=8798).start()
    results: list[tuple[str, bool | None, str]] = []
    try:
        s.seed()
        s.start_sensor()
        s.wait_settled()
        t0 = s.add_live_server()
        live = wait_for(lambda: s.agent_with("inbox-digest", lambda a: a.get("cluster_id")), 40)
        cid = live["cluster_id"] if live else None

        def written():
            d = s.api.get(f"/api/clusters/{cid}/draft").json()
            ok = d["written_by"] == "llm" and not d["writing"] and any(v["agent_id"] == live["id"] for v in d["variants"])
            return d if ok else None

        d = wait_for(written, 60, 0.5) if cid else None
        t_draft = time.time() - t0
        results.append(("consolidated draft includes the live agent as a variant", bool(d),
                        f"{t_draft:.1f}s after the config edit" if d else "timeout"))
        if not d:
            return 1
        create = next(r for r in d["api_requests"] if r["step"] == "create_agent")["body"]
        results.append(("agent create body uses only documented fields + limits",
                        set(create) <= AGENT_FIELDS and len(create["name"]) <= 80 and len(create["description"]) <= 800
                        and len(create["instruction"]) <= 50000, f"{sorted(create)}"))
        scheds = [r["body"] for r in d["api_requests"] if r["url"].endswith("/automations/v1")]
        results.append(("cron schedules become Scheduled Tasks", bool(scheds) and all(b["frequency"] in FREQ for b in scheds),
                        "; ".join(f"{b['name']}: {b['frequency']} {b.get('timeOfDay')}" for b in scheds)))
        results.append(("integrations mapped to native Langdock integrations",
                        {"Gmail", "Microsoft Outlook", "HubSpot", "Slack"} <= {i["langdock_integration"] for i in d["integrations"]},
                        ", ".join(f"{i['langdock_integration']} ({len(i['used_by'])})" for i in d["integrations"])))
        blocked = s.api.post(f"/api/clusters/{cid}/create")
        results.append(("create is blocked until approved", blocked.status_code == 409, f"HTTP {blocked.status_code}"))
        ap = s.api.post(f"/api/clusters/{cid}/approve", json={"approved_by": "it.admin@nordlicht.example"}).json()
        cr = s.api.post(f"/api/clusters/{cid}/create").json()
        expect = "created" if os.environ.get("LANGDOCK_API_KEY") else "dry_run"
        results.append((f"approve -> create ({expect})", ap["status"] == "approved" and cr["status"] == expect,
                        cr.get("reason") or str(cr.get("log"))[:200]))
        md = s.api.get(f"/api/clusters/{cid}/export.md")
        js = s.api.get(f"/api/clusters/{cid}/export.json")
        results.append(("exports: importable JSON + human-readable spec", md.status_code == js.status_code == 200
                        and md.text.startswith("# ") and "api_requests" in js.json(), f"{len(md.text)} chars of Markdown"))

        print(f"\nDraft: {d['emoji']} {d['name']} replaces {d['replaces_count']} agents of {d['people']} people")
        for v in d["variants"]:
            print(f"  - {v['owner']:<16} {v['department'] or '-':<10} {v['diff']}")
        print("\n" + d["instruction"][:1500])
    finally:
        s.stop()
    print("\nPhase 5 acceptance")
    ok = True
    for name, passed, detail in results:
        ok &= passed is not False
        print(f"  [{'SKIP' if passed is None else 'PASS' if passed else 'FAIL'}] {name}  {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
