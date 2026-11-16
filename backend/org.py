"""Who is who (directory) and what is allowed (policy).

The directory is the simulated IdP export in fixtures/directory.yaml. Laptops that
aren't in it (the presenter's own machine on camera) resolve to a person built from
DEMO_OWNER_NAME / DEMO_OWNER_EMAIL / DEMO_OWNER_DEPARTMENT, else the local username.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = Path(os.environ.get("ORG_DIRECTORY", ROOT / "fixtures" / "directory.yaml"))
POLICY = Path(os.environ.get("ORG_POLICY", ROOT / "fixtures" / "policy.yaml"))


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    email: str | None = None
    department: str | None = None
    title: str | None = None
    known: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "email": self.email, "department": self.department,
                "title": self.title, "in_directory": self.known}


@dataclass
class Directory:
    by_device: dict[tuple[str, str], Person] = field(default_factory=dict)
    by_email: dict[str, Person] = field(default_factory=dict)
    by_id: dict[str, Person] = field(default_factory=dict)
    service_accounts: dict[str, str] = field(default_factory=dict)  # name -> owner person id

    @classmethod
    def load(cls, path: Path = DIRECTORY) -> "Directory":
        d = cls()
        data = yaml.safe_load(path.read_text()) if path.exists() else {}
        for p in data.get("people", []):
            person = Person(p["id"], p["name"], p.get("email"), p.get("department"), p.get("title"))
            d.by_id[person.id] = person
            for e in [p.get("email"), *p.get("aliases", [])]:
                if e:
                    d.by_email[e.lower()] = person
            for dev in p.get("devices", []):
                d.by_device[(dev["host"], dev["user"])] = person
        for sa in data.get("service_accounts", []):
            d.service_accounts[sa["id"]] = sa.get("owner")
        return d

    def for_device(self, host: str, user: str) -> Person:
        if (host, user) in self.by_device:
            return self.by_device[(host, user)]
        name = os.environ.get("DEMO_OWNER_NAME") or user
        email = os.environ.get("DEMO_OWNER_EMAIL")
        return Person(f"{user}@{host}", name, email, os.environ.get("DEMO_OWNER_DEPARTMENT"),
                      os.environ.get("DEMO_OWNER_TITLE"), known=False)

    def for_email(self, email: str | None) -> Person:
        if email and email.lower() in self.by_email:
            return self.by_email[email.lower()]
        return Person(email or "unknown", (email or "unknown").split("@")[0], email, known=False)


@dataclass
class Policy:
    sanctioned_apps: set[str]
    approved_llm_providers: set[str]
    systems_of_record: set[str]
    company_data_systems: set[str]

    @classmethod
    def load(cls, path: Path = POLICY) -> "Policy":
        data = yaml.safe_load(path.read_text()) if path.exists() else {}
        return cls(set(data.get("sanctioned_apps", [])), set(data.get("approved_llm_providers", [])),
                   set(data.get("systems_of_record", [])), set(data.get("company_data_systems", [])))


@lru_cache(maxsize=1)
def directory() -> Directory:
    return Directory.load()


@lru_cache(maxsize=1)
def policy() -> Policy:
    return Policy.load()
