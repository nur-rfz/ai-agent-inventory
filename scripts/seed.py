"""Seed the backend with the simulated org.

    python scripts/seed.py                 # post everything at once (history view)
    python scripts/seed.py --replay        # stream with realistic pacing (~30s) so the UI fills live
    python scripts/seed.py --replay --speed 3
    python scripts/seed.py --reset         # wipe the backend first

Sensor signals come from fixtures/signals.jsonl (built by fixtures/build.py).
Identity logs are vendor-shaped exports in fixtures/identity/, normalized by
backend/identity.py at seed time. All timestamps are shifted by whole weeks so the
newest event lands just before now: weekdays and clock times stay realistic.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import identity  # noqa: E402

FIX = ROOT / "fixtures"
# When each identity connector "polls" during the replay (seconds from start).
IDENTITY = [
    ("google_workspace", "google_token_activity.json", 3.5),
    ("entra", "entra_directory_audits.json", 8.5),
    ("openai", "openai_audit_logs.json", 13.0),
]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def week_shift(latest: datetime, now: datetime | None = None) -> timedelta:
    now = now or datetime.now(timezone.utc)
    return timedelta(weeks=(now - latest) // timedelta(weeks=1))


def load(now: datetime | None = None) -> list[tuple[float, dict]]:
    rows = [json.loads(line) for line in (FIX / "signals.jsonl").read_text().splitlines() if line.strip()]
    docs = [(prov, json.loads((FIX / "identity" / fn).read_text()), t) for prov, fn, t in IDENTITY]
    latest = max([_parse(r["signal"]["ts"]) for r in rows] + [identity.latest_time(p, d) for p, d, _ in docs])
    shift = week_shift(latest, now)

    out: list[tuple[float, dict]] = []
    for r in rows:
        s = dict(r["signal"])
        s["ts"] = (_parse(s["ts"]) + shift).isoformat(timespec="milliseconds")
        raw = dict(s["raw"])
        if isinstance(raw.get("started_at"), (int, float)):
            raw["started_at"] = raw["started_at"] + shift.total_seconds()
        s["raw"] = raw
        out.append((r["t"], s))
    for prov, doc, t in docs:
        for i, sig in enumerate(identity.normalize(prov, doc, shift)):
            out.append((t + i * 0.2, sig.model_dump()))
    out.sort(key=lambda x: x[0])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default=os.environ.get("BACKEND_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--replay", action="store_true", help="stream with timing instead of one bulk post")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    ap.add_argument("--reset", action="store_true", help="wipe backend data first")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    events = load()
    if a.dry_run:
        kinds: dict[str, int] = {}
        for _, s in events:
            kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        print(f"{len(events)} signals over {events[-1][0]:.1f}s of replay: {kinds}")
        print(f"newest event: {max(s['ts'] for _, s in events)}")
        return 0

    api = httpx.Client(base_url=a.backend, timeout=30)

    def post(batch: list[dict]) -> None:
        for attempt in range(5):  # the backend can stall briefly while it loads the embedding model
            try:
                api.post("/api/signals", json=batch).raise_for_status()
                return
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt == 4:
                    raise
                time.sleep(1 + attempt)

    if a.reset:
        api.post("/api/admin/reset").raise_for_status()
        print("backend reset")
    if not a.replay:
        for i in range(0, len(events), 200):
            post([s for _, s in events[i:i + 200]])
        print(f"seeded {len(events)} signals")
        return 0

    start = time.monotonic()
    i, sent = 0, 0
    while i < len(events):
        due = (time.monotonic() - start) * a.speed
        batch = []
        while i < len(events) and events[i][0] <= due:
            batch.append(events[i][1])
            i += 1
        if batch:
            post(batch)
            sent += len(batch)
            hosts = sorted({s["host"] for s in batch})
            print(f"  t={due:5.1f}s  +{len(batch):3d}  ({', '.join(hosts)})", flush=True)
        else:
            time.sleep(0.05)
    print(f"replayed {sent} signals in {time.monotonic() - start:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
