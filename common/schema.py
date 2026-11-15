"""Wire format for signals (sensor -> backend). Shared by both sides."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SignalKind = Literal["mcp_config", "process", "net_conn", "mcp_probe", "code_import", "oauth_grant", "api_key", "schedule"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def fingerprint(*parts: Any) -> str:
    """Stable sha256 over JSON-encoded parts. Callers pass REDACTED values only."""
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class SignalIn(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = Field(default_factory=now_iso)
    source: str            # e.g. "sensor:mcp_config", "fixture:entra", "sensor:codescan"
    host: str
    user: str
    kind: SignalKind
    raw: dict[str, Any]    # already redacted
    fingerprint: str       # what this signal is about (server fp, process fp, ...)
