"""Phase 4 acceptance: duplicate clusters, and the live agent joins an existing one.

  python scripts/acceptance_phase4.py
"""

from __future__ import annotations

import sys
import time

from harness import Stack, wait_for


def main() -> int:
    s = Stack(port=8797).start()
    results: list[tuple[str, bool | None, str]] = []
    try:
        s.seed()
        s.start_sensor()
        settled = s.wait_settled()
        results.append(("background cards + cluster confirmations settle", settled, ""))
        clusters = s.clusters()
        by = {a["id"]: a for a in s.agents()}
        email = next((c for c in clusters if any("customer-email-digest" in str(by[i]["members"]) for i in c["member_agent_ids"])), None)
        pipeline = next((c for c in clusters if c is not email), None)
        results.append(("'3 people built the same agent': email summarizer cluster (Maya, Jonas, Priya)",
                        bool(email) and email["people"] == 3 and email["status"] == "confirmed",
                        f"{email['label']}: {[m['owner'] for m in email['members']]}" if email else "missing"))
        results.append(("pipeline report cluster (Lena, Tom)", bool(pipeline) and pipeline["people"] == 2,
                        f"{pipeline['label']}: {[m['owner'] for m in pipeline['members']]}" if pipeline else "missing"))
        results.append(("coding / SaaS agents are not clustered",
                        not any(by[i]["classification"] != "replace" for c in clusters for i in c["member_agent_ids"]), ""))

        t0 = s.add_live_server()
        card = wait_for(lambda: s.agent_with("inbox-digest", lambda a: a["status"] in ("enriched", "needs_review")), 30)
        t_card = time.time() - t0
        joined = wait_for(lambda: next((c for c in s.clusters() if card and card["id"] in c["member_agent_ids"]
                                         and c["status"] == "confirmed"), None), 40)
        t_join = time.time() - t0
        results.append(("live agent joins an existing cluster (same cluster id, confirmed)",
                        bool(joined) and email is not None and joined["id"] == email["id"],
                        f"card {t_card:.1f}s, joined {joined['label'] if joined else '-'} at {t_join:.1f}s "
                        f"({joined['people'] if joined else 0} people)"))
        if joined:
            by = {a["id"]: a for a in s.agents()}
            print(f"\nCluster after the live edit: {joined['label']} ({joined['people']} people, confidence {joined['confidence']})")
            print("  base purpose:", joined["base_purpose"])
            print("  shared:", joined["shared_capabilities"])
            for v in joined["variants"]:
                a = by[v["agent_id"]]
                print(f"   - {a['owner']['name']:<16} {a['name']:<36} {v['diff']}")
    finally:
        s.stop()
    print("\nPhase 4 acceptance")
    ok = True
    for name, passed, detail in results:
        ok &= passed is not False
        print(f"  [{'SKIP' if passed is None else 'PASS' if passed else 'FAIL'}] {name}  {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
