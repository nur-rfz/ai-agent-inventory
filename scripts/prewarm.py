"""Pre-warm the LLM caches for the simulated org (run once before recording).

Replays the fixtures into the demo database at demo speed, so the cards, cluster
confirmations and Langdock drafts for every state the replay passes through are
cached. Only the agent you add live on camera then calls the LLM.

    python scripts/prewarm.py            # uses data/inventory.db (the demo DB)
"""

from __future__ import annotations

import sys
import time

from harness import ROOT, Stack, wait_for


def main() -> int:
    db = ROOT / "data" / "inventory.db"
    db.parent.mkdir(exist_ok=True)
    s = Stack(port=8799, db=db, replay_speed=1.0).start()
    try:
        st = s.stats()
        if not st["enrichment"]["enabled"]:
            print(f"LLM disabled: {st['enrichment']['disabled_reason']}. Nothing to pre-warm.")
            return 1
        t0 = time.time()
        print("replaying the simulated org at demo speed…", flush=True)
        s.seed()
        ok = s.wait_settled(300)

        def drafts_written():
            cl = s.clusters()
            return cl if cl and all((c.get("draft") or {}).get("written_by") == "llm" and not c["draft"]["writing"]
                                    for c in cl) else None

        cl = wait_for(drafts_written, 180, 1.0)
        st = s.stats()
        print(f"{'done' if ok and cl else 'INCOMPLETE'} in {time.time() - t0:.0f}s: {st['agents']} agents, "
              f"{len(cl or [])} clusters with drafts, {st['enrichment']['calls']} card calls, "
              f"{st['clustering']['confirm_calls']} cluster calls")
        return 0 if ok and cl else 1
    finally:
        s.stop()


if __name__ == "__main__":
    sys.exit(main())
