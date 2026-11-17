"""Backend: ingest signals, resolve agents, enrich with an LLM, broadcast over SSE.

uvicorn backend.app:app --port 8787
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from sqlmodel import delete, select

from common.redact import scrub
from common.schema import SignalIn, now_iso

from . import enrich as enrich_mod
from .db import Signal, init_db, session
from .cluster import ClusterCacheStore, ClusterService
from .enrich import Enricher, apply_card
from .langdock import DraftService, execute, to_markdown
from .org import directory, policy
from .resolve import build_agents, public

log = logging.getLogger("backend")
logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(name)s %(message)s", datefmt="%H:%M:%S")
SETTLE_S = 0.4  # evidence must be unchanged this long before we spend an LLM call


class Broker:
    """Fan-out of new events to SSE subscribers."""

    def __init__(self) -> None:
        self.subs: set[asyncio.Queue[str]] = set()

    def publish(self, event: str, data: dict) -> None:
        msg = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def stream(self, request: Request) -> AsyncIterator[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
        self.subs.add(q)
        try:
            yield "event: hello\ndata: {}\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            self.subs.discard(q)


def _is_fresh(agent: dict, window_s: float = 300) -> bool:
    """First seen in the last few minutes = live discovery; seeded history is days old."""
    from datetime import datetime, timezone

    fs = agent.get("first_seen")
    return bool(fs) and (datetime.now(timezone.utc) - datetime.fromisoformat(fs)).total_seconds() < window_s


def _probe_settled(agent: dict) -> bool:
    """Every MCP member has a probe outcome (ok / error / skipped), so the evidence is complete."""
    kinds = [e["kind"] for e in agent["_evidence"]["evidence"]]
    servers = kinds.count("mcp_server_config")
    return servers > 0 and kinds.count("mcp_tools") + kinds.count("mcp_probe") >= servers


class Inventory:
    """Signals in memory; agents recomputed (debounced) after every ingest batch."""

    def __init__(self, broker: Broker):
        self.broker = broker
        self.signals: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.raw_agents: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        self.enricher: Enricher | None = None
        self.clusterer: ClusterService | None = None
        self.clusters: list[dict] = []
        self.drafts: DraftService | None = None
        self.draft_cache: dict[str, dict] = {}
        self._pending: asyncio.Task | None = None
        self._seen_hash: dict[str, tuple[str, float]] = {}
        self.last_recompute_ms = 0.0

    def load(self) -> None:
        with session() as s:
            self.signals = [r.model_dump() for r in s.exec(select(Signal).order_by(Signal.ts))]
        self.by_id = {x["id"]: x for x in self.signals}

    def add(self, rows: list[dict]) -> None:
        self.signals.extend(rows)
        self.by_id.update({x["id"]: x for x in rows})
        self.schedule()

    def clear(self) -> None:
        self.signals, self.by_id = [], {}
        self.schedule()

    def schedule(self, delay: float = 0.1) -> None:
        if self._pending and not self._pending.done():
            return
        self._pending = asyncio.create_task(self._later(delay))

    async def _later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self.recompute()

    def recompute(self) -> None:
        t0 = time.perf_counter()
        raw = build_agents(self.signals, directory(), policy())
        status = self.enricher.status() if self.enricher else {"enabled": False}
        new: dict[str, dict] = {}
        now = time.monotonic()
        for a in raw:
            card = self.enricher.card_for(a) if self.enricher else None
            apply_card(a, card, status)
            if not card and self.enricher and not self.enricher.disabled_reason:
                h, since = self._seen_hash.get(a["id"], (None, now))
                fresh = _is_fresh(a)
                if fresh and a["id"] not in self._seen_hash:
                    self.enricher.warm()
                if fresh and _probe_settled(a):  # live agent with its tools in: nothing left to wait for
                    self._seen_hash[a["id"]] = (a["evidence_hash"], now)
                    self.enricher.request(a, hot=True)
                elif h != a["evidence_hash"]:
                    self._seen_hash[a["id"]] = (a["evidence_hash"], now)
                    asyncio.get_running_loop().call_later(SETTLE_S + 0.05, self.schedule, 0)
                elif now - since >= SETTLE_S:
                    self.enricher.request(a, hot=_is_fresh(a))
                if enrich_mod.cache_key(a["evidence_hash"]) in self.enricher.inflight:
                    a["status"] = "enriching"
            new[a["id"]] = a
        self.raw_agents = new
        pub = {k: public(v) for k, v in new.items()}
        clusters = self.clusterer.compute(pub)[0] if self.clusterer else []
        in_cluster = {aid: c["id"] for c in clusters for aid in c["member_agent_ids"]}
        for aid, a in pub.items():
            a["cluster_id"] = in_cluster.get(aid)
        if self.drafts:
            for c in clusters:
                d = self.drafts.draft(c, [pub[i] for i in c["member_agent_ids"]], self.raw_agents)
                self.draft_cache[c["id"]] = d
                c["draft"] = {"status": d["status"], "writing": d["writing"], "name": d["name"],
                              "written_by": d["written_by"]}
        if clusters != self.clusters:
            self.broker.publish("clusters", clusters)
        self.clusters = clusters
        for aid, a in pub.items():
            if self.agents.get(aid) != a:
                self.broker.publish("agent", a)
        for aid in set(self.agents) - set(pub):
            self.broker.publish("agent_removed", {"id": aid})
        self.agents = pub
        self.last_recompute_ms = (time.perf_counter() - t0) * 1000
        self.broker.publish("stats", stats(self))

    async def on_card(self, agent_id: str) -> None:
        self.schedule(0)


def stats(inv: Inventory) -> dict:
    agents = list(inv.agents.values())
    total = len(agents)
    unmanaged = [a for a in agents if a["classification"] != "ok"]
    risk = {lvl: sum(a["risk"]["level"] == lvl for a in agents) for lvl in ("high", "medium", "low", "none")}
    return {
        "agents": total,
        "unmanaged": len(unmanaged),
        "unmanaged_pct": round(100 * len(unmanaged) / total) if total else 0,
        "replace": sum(a["classification"] == "replace" for a in agents),
        "govern_in_place": sum(a["classification"] == "govern_in_place" for a in agents),
        "ok": sum(a["classification"] == "ok" for a in agents),
        "people": len({a["owner"]["id"] for a in agents}),
        "risk": risk,
        "signals": len(inv.signals),
        "duplicate_clusters": len(inv.clusters),
        "agents_in_clusters": sum(len(c["member_agent_ids"]) for c in inv.clusters),
        "agents_consolidated": sum(len(c["member_agent_ids"]) - 1 for c in inv.clusters),
        "enrichment": inv.enricher.status() if inv.enricher else None,
        "clustering": inv.clusterer.status() if inv.clusterer else None,
    }


broker = Broker()
inventory = Inventory(broker)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    inventory.load()
    pol = asdict(policy())
    inventory.enricher = Enricher(inventory.on_card, {k: sorted(v) for k, v in pol.items()})
    inventory.enricher.start()
    inventory.clusterer = ClusterService(lambda: inventory.schedule(0), ClusterCacheStore())
    inventory.clusterer.start()
    inventory.drafts = DraftService(lambda: inventory.schedule(0))
    inventory.recompute()
    yield
    await inventory.enricher.stop()


app = FastAPI(title="Shadow Agent Discovery", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/signals")
async def ingest(signals: list[SignalIn]) -> dict:
    received = now_iso()
    stored = []
    with session() as s:
        for sig in signals:
            if sig.id in inventory.by_id or s.get(Signal, sig.id):
                continue
            # defence in depth: fixtures and third-party senders get scrubbed too
            row = Signal(**{**sig.model_dump(), "raw": scrub(sig.raw)}, received_at=received)
            s.add(row)
            stored.append(row.model_dump())
        s.commit()
    for row in stored:
        broker.publish("signal", row)
    if stored:
        inventory.add(stored)
    return {"stored": len(stored)}


@app.post("/api/admin/reset")
async def reset(keep_live_cache: bool = False) -> dict:
    """Demo helper: wipe signals. LLM caches for the simulated org are kept (pre-warm); by default
    everything cached about other agents (the live one) is dropped so it calls the LLM on camera.
    Local prototype only, no auth."""
    from scripts.seed import load

    from .db import ClusterCache, DraftCache
    from .enrich import EnrichmentCache

    with session() as s:
        n = len(inventory.signals)
        s.exec(delete(Signal))
        s.commit()
    forgotten = 0
    if not keep_live_cache:
        fixture_ids = {a["id"] for a in build_agents([x for _, x in load()], directory(), policy())}
        e, c, d = inventory.enricher, inventory.clusterer, inventory.drafts
        with session() as s:
            for row in s.query(EnrichmentCache).all():
                if row.agent_id not in fixture_ids:
                    s.delete(row); e.cards.pop(row.key, None); forgotten += 1
            for row in s.query(ClusterCache).all():
                ids = {i for g in row.result.get("groups", []) for i in g["member_ids"]}
                if ids - fixture_ids:
                    s.delete(row); c.confirmed.pop(row.key, None); forgotten += 1
            for row in s.query(DraftCache).all():
                if set(row.written.get("_member_ids", [])) - fixture_ids:
                    s.delete(row); d.written.pop(row.key, None); forgotten += 1
            s.commit()
        inventory._seen_hash.clear()
    from .db import DraftState

    with session() as s:  # every demo starts unapproved
        s.exec(delete(DraftState))
        s.commit()
    inventory.drafts.state.clear()
    inventory.clear()
    broker.publish("reset", {})
    return {"deleted": n, "forgot_live_cache_entries": forgotten}


@app.get("/api/signals")
def list_signals(kind: str | None = None, limit: int = 200) -> list[dict]:
    with session() as s:
        q = select(Signal).order_by(Signal.ts.desc()).limit(limit)
        if kind:
            q = q.where(Signal.kind == kind)
        return [r.model_dump() for r in s.exec(q)]


@app.get("/api/agents")
def list_agents() -> list[dict]:
    return sorted(inventory.agents.values(), key=lambda a: a["last_seen"] or "", reverse=True)


def _graph(a: dict) -> dict:
    nodes = [{"id": f"p:{a['owner']['id']}", "type": "person", "label": a["owner"]["name"]},
             {"id": f"a:{a['id']}", "type": "agent", "label": a["name"]}]
    edges = [{"from": f"p:{a['owner']['id']}", "to": f"a:{a['id']}", "label": "built"}]
    for m in a["members"] + a["linked_identity"]:
        mid = f"m:{m['fingerprint']}"
        nodes.append({"id": mid, "type": m["kind"], "label": m["name"],
                      "tools": [t["name"] for t in a["tools"] if t["server"] == m["name"]]})
        edges.append({"from": f"a:{a['id']}", "to": mid, "label": "uses"})
    for r in a["data_reach"]:
        sid = f"s:{r['system']}"
        nodes.append({"id": sid, "type": "system", "label": r["system"]})
        via_members = {t["server"] for t in a["tools"] if t.get("system") == r["system"]}
        targets = [f"m:{m['fingerprint']}" for m in a["members"] if m["name"] in via_members]
        if not targets:  # no tool says which server: hang it off the agent's own script/server, never an identity app
            own = [f"m:{m['fingerprint']}" for m in a["members"] if m["kind"] in ("script", "mcp_server")]
            targets = own[:1] or [f"a:{a['id']}"]
        for t in targets:
            edges.append({"from": t, "to": sid, "label": r["access"]})
    return {"nodes": nodes, "edges": edges}


@app.get("/api/agents/{agent_id}")
def agent_detail(agent_id: str) -> dict:
    a = inventory.agents.get(agent_id)
    if not a:
        raise HTTPException(404, "unknown agent")
    sigs = sorted((inventory.by_id[i] for i in a["evidence_signal_ids"] if i in inventory.by_id),
                  key=lambda s: s["ts"], reverse=True)
    raw = inventory.raw_agents.get(agent_id, {})
    return {**a, "evidence": sigs[:400], "evidence_total": len(sigs), "graph": _graph(a),
            "evidence_bundle": raw.get("_evidence")}


@app.post("/api/agents/{agent_id}/enrich")
async def enrich_now(agent_id: str) -> dict:
    a = inventory.raw_agents.get(agent_id)
    if not a:
        raise HTTPException(404, "unknown agent")
    inventory.enricher.request(a)
    return inventory.enricher.status()


@app.get("/api/clusters")
def list_clusters() -> list[dict]:
    return inventory.clusters


@app.get("/api/clusters/{cluster_id}")
def cluster_detail(cluster_id: str) -> dict:
    c = next((c for c in inventory.clusters if c["id"] == cluster_id), None)
    if not c:
        raise HTTPException(404, "unknown cluster")
    return {**c, "agents": [inventory.agents[i] for i in c["member_agent_ids"] if i in inventory.agents]}


def _draft(cluster_id: str) -> dict:
    inventory.recompute()
    d = inventory.draft_cache.get(cluster_id)
    if not d:
        raise HTTPException(404, "unknown cluster")
    return d


@app.get("/api/clusters/{cluster_id}/draft")
def get_draft(cluster_id: str) -> dict:
    return _draft(cluster_id)


@app.post("/api/clusters/{cluster_id}/approve")
async def approve_draft(cluster_id: str, request: Request) -> dict:
    body = await request.json() if (await request.body()) else {}
    d = _draft(cluster_id)
    if d["writing"]:
        raise HTTPException(409, "draft is still being written")
    inventory.drafts.set_state(cluster_id, status="approved", draft_key=d["draft_key"],
                               approved_by=(body or {}).get("approved_by", "IT admin"), approved_at=now_iso())
    inventory.schedule(0)
    return _draft(cluster_id)


@app.post("/api/clusters/{cluster_id}/create")
async def create_in_langdock(cluster_id: str) -> dict:
    """Send the planned requests to Langdock. Requires approval of this exact draft + LANGDOCK_API_KEY."""
    import os

    d = _draft(cluster_id)
    if d["status"] not in ("approved", "failed"):
        raise HTTPException(409, "approve the draft first (and re-approve after it changes)")
    key = os.environ.get("LANGDOCK_API_KEY")
    if not key:
        return {"status": "dry_run", "reason": "LANGDOCK_API_KEY not set: nothing was sent. These are the exact requests:",
                "requests": d["api_requests"], "manual_steps": d["manual_steps"]}
    log_ = await execute(d["api_requests"], key)
    ok = all(200 <= e["status"] < 300 for e in log_) and len(log_) == len(d["api_requests"])
    agent_id = next((e.get("agent_id") for e in log_ if e.get("agent_id")), None)
    inventory.drafts.set_state(cluster_id, status="created" if ok else "failed", created_log=log_,
                               langdock_agent_id=agent_id, draft_key=d["draft_key"])
    inventory.schedule(0)
    return {"status": "created" if ok else "failed", "log": log_, "langdock_agent_id": agent_id}


@app.get("/api/clusters/{cluster_id}/export.json")
def export_json(cluster_id: str) -> JSONResponse:
    d = _draft(cluster_id)
    return JSONResponse({k: v for k, v in d.items() if k not in ("writing", "write_error")},
                        headers={"Content-Disposition": f'attachment; filename="langdock-{cluster_id}.json"'})


@app.get("/api/clusters/{cluster_id}/export.md")
def export_md(cluster_id: str) -> PlainTextResponse:
    return PlainTextResponse(to_markdown(_draft(cluster_id)), media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="langdock-{cluster_id}.md"'})


@app.get("/api/stats")
def get_stats() -> dict:
    return {**stats(inventory), "recompute_ms": round(inventory.last_recompute_ms, 1)}


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    return StreamingResponse(broker.stream(request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Serve the built UI (ui/dist) at / so the demo needs one web process. Dev: `npm run dev` in ui/.
_UI = __import__("pathlib").Path(__file__).resolve().parent.parent / "ui" / "dist"
if _UI.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_UI, html=True), name="ui")
