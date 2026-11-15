"""Backend: ingest signals, store them, broadcast over SSE.

uvicorn backend.app:app --port 8787
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlmodel import select

from common.redact import scrub
from common.schema import SignalIn, now_iso

from .db import Signal, init_db, session


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
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
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


broker = Broker()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


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
            if s.get(Signal, sig.id):
                continue
            # defence in depth: fixtures and third-party senders get scrubbed too
            row = Signal(**{**sig.model_dump(), "raw": scrub(sig.raw)}, received_at=received)
            s.add(row)
            stored.append(row.model_dump())
        s.commit()
    for row in stored:
        broker.publish("signal", row)
    return {"stored": len(stored)}


@app.get("/api/signals")
def list_signals(kind: str | None = None, limit: int = 200) -> list[dict]:
    with session() as s:
        q = select(Signal).order_by(Signal.ts.desc()).limit(limit)
        if kind:
            q = q.where(Signal.kind == kind)
        return [r.model_dump() for r in s.exec(q)]


@app.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    return StreamingResponse(broker.stream(request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
