from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Column
from sqlalchemy.types import JSON
from sqlmodel import Field, Session, SQLModel, create_engine

DB_PATH = Path(os.environ.get("INVENTORY_DB", Path(__file__).resolve().parent.parent / "data" / "inventory.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


class Signal(SQLModel, table=True):
    id: str = Field(primary_key=True)
    ts: str = Field(index=True)
    received_at: str
    source: str
    host: str
    user: str
    kind: str = Field(index=True)
    raw: dict = Field(sa_column=Column(JSON))
    fingerprint: str = Field(index=True)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def session() -> Session:
    return Session(engine)
