"""Phase 4: find agents that different people built for the same job.

1. Candidates: for every enriched `replace` agent, similarity =
     0.5  * cosine(embedding of job + name + capabilities)      [all-MiniLM-L6-v2, local]
   + 0.25 * same primary category of the core job              [email / crm / documents / ...]
   + 0.25 * Jaccard(system categories)                          [Gmail ~ Outlook = email, ...]
   Average-linkage agglomeration at CANDIDATE_THRESHOLD (no chaining through bridge agents).
   (Tuned on the fixtures: plain embeddings put true duplicates at 0.38-0.71 and
   unrelated agents up to 0.48, so no single cosine cutoff works; the category term
   separates them and the LLM makes the final call.)
2. Confirmation: one LLM call per candidate component returns the genuine duplicate
   groups, a base purpose, the shared capabilities and what each member does
   differently ("uses Outlook instead of Gmail", "adds CRM lookup").
   Cached by membership + member evidence; works without an LLM as unconfirmed candidates.
3. Stable ids: a cluster keeps the id of its earliest member, so the live agent
   joining "Customer email summarizer" updates that cluster instead of creating a new one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from typing import Any, Callable

import numpy as np

from common.redact import scrub
from common.schema import now_iso

from . import llm

log = logging.getLogger("cluster")

EMBED_MODEL = os.environ.get("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
W_EMBED, W_PRIMARY, W_CATEGORY = 0.5, 0.25, 0.25
CANDIDATE_THRESHOLD = float(os.environ.get("CLUSTER_THRESHOLD", "0.45"))
PROMPT_VERSION = "c3"

SYSTEM_CATEGORY = {"Gmail": "email", "Salesforce": "crm", "HubSpot": "crm", "Slack": "chat", "Google Drive": "files",
                   "Notion": "docs", "Confluence": "docs", "Jira": "tickets", "Zendesk": "tickets", "Intercom": "tickets",
                   "Stripe": "billing"}
CAP_CATEGORY = [(r"\b(e-?mails?|inbox(es)?|mailbox(es)?|mails?)\b", "email"), (r"\b(crm|account|opportunit|deal|pipeline)", "crm"),
                (r"\b(slack|channel|teams message)", "chat"), (r"\b(file|document|sharepoint|onedrive|spreadsheet|xlsx)", "files")]


def categories(agent: dict) -> set[str]:
    cats = {SYSTEM_CATEGORY[r["system"]] for r in agent["data_reach"] if r["system"] in SYSTEM_CATEGORY}
    text = " ".join([agent.get("job") or "", agent.get("name") or ""] + (agent.get("capabilities") or [])).lower()
    for pat, cat in CAP_CATEGORY:  # Microsoft 365 is mail *or* files: decide from what the agent does
        if re.search(pat, text):
            cats.add(cat)
    return cats


PRIMARY_CATEGORY = [(r"\b(e-?mails?|inbox(es)?|mailbox(es)?|mails?)\b", "email"),
                    (r"(pipeline|sales|crm|deal|opportunit|salesforce|hubspot|forecast)", "crm"),
                    (r"(pull request|code|repo|bug|incident)", "engineering"),
                    (r"(document|file|knowledge|contract|policy)", "documents"),
                    (r"(meeting|calendar|call notes)", "meetings")]


def primary_category(agent: dict) -> str | None:
    """The category of the core job itself ("triage customer support inbox" -> email)."""
    text = f"{agent.get('job') or ''} {agent.get('name') or ''}".lower()
    return next((cat for pat, cat in PRIMARY_CATEGORY if re.search(pat, text)), None)


def embed_text(agent: dict) -> str:
    caps = "; ".join(agent.get("capabilities") or [])
    return f"{agent.get('job') or ''}. {agent['name']}. {caps}"


class Embedder:
    """Lazy, thread-safe local embedding model with a text -> vector cache."""

    def __init__(self) -> None:
        self.model = None
        self.error: str | None = None
        self.cache: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()

    def load(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer

            t0 = time.time()
            m = SentenceTransformer(EMBED_MODEL, device="cpu")
            m.encode(["warm up"], normalize_embeddings=True)
            self.model = m
            log.info("embedding model ready in %.1fs", time.time() - t0)
        except Exception as e:  # offline, blocked host, ...
            self.error = f"{type(e).__name__}: {str(e)[:160]}"
            log.warning("embedding model unavailable: %s", self.error)

    @property
    def ready(self) -> bool:
        return self.model is not None

    def encode(self, texts: list[str]) -> np.ndarray:
        with self._lock:
            missing = [t for t in dict.fromkeys(texts) if t not in self.cache]
            if missing:
                for t, v in zip(missing, self.model.encode(missing, normalize_embeddings=True)):
                    self.cache[t] = v
            return np.stack([self.cache[t] for t in texts])


def candidate_groups(agents: list[dict], embedder: Embedder) -> tuple[list[list[str]], dict[tuple[str, str], dict]]:
    """Average-linkage agglomeration over the pair scores (groups of 2+). Returns (groups, pair scores).

    Average linkage (not connected components): an agent that touches two jobs (e.g. an inbox
    triager that also posts to Slack) can't chain two unrelated clusters together.
    """
    pool = [a for a in agents if a["classification"] == "replace" and a.get("job") and a.get("capabilities")]
    if len(pool) < 2 or not embedder.ready:
        return [], {}
    vecs = embedder.encode([embed_text(a) for a in pool])
    sims = vecs @ vecs.T
    cats = [categories(a) for a in pool]
    prim = [primary_category(a) for a in pool]
    pairs: dict[tuple[str, str], dict] = {}
    score = np.zeros((len(pool), len(pool)))
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            union = cats[i] | cats[j]
            jac = len(cats[i] & cats[j]) / len(union) if union else 0.0
            same_primary = 1.0 if prim[i] and prim[i] == prim[j] else 0.0
            sc = W_EMBED * float(sims[i, j]) + W_PRIMARY * same_primary + W_CATEGORY * jac
            score[i, j] = score[j, i] = sc
            key = tuple(sorted((pool[i]["id"], pool[j]["id"])))
            pairs[key] = {"score": round(sc, 3), "embedding": round(float(sims[i, j]), 3), "category_overlap": round(jac, 2),
                          "same_primary_category": bool(same_primary), "shared_categories": sorted(cats[i] & cats[j])}
    groups: list[list[int]] = [[i] for i in range(len(pool))]
    while True:
        best, pair = CANDIDATE_THRESHOLD, None
        for x in range(len(groups)):
            for y in range(x + 1, len(groups)):
                avg = float(np.mean([score[i, j] for i in groups[x] for j in groups[y]]))
                if avg >= best:
                    best, pair = avg, (x, y)
        if not pair:
            break
        x, y = pair
        groups[x] = groups[x] + groups[y]
        del groups[y]
    return [sorted(pool[i]["id"] for i in g) for g in groups if len(g) > 1], pairs


# --- LLM confirmation -----------------------------------------------------------------

_STR = {"type": "string"}
CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["groups"],
    "properties": {"groups": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["members", "label", "base_purpose", "shared_capabilities", "variants", "rationale", "confidence"],
        "properties": {
            "members": {"type": "array", "items": _STR},
            "label": _STR,
            "base_purpose": _STR,
            "shared_capabilities": {"type": "array", "items": _STR},
            "variants": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["member", "diff"],
                "properties": {"member": _STR, "diff": _STR}}},
            "rationale": _STR,
            "confidence": {"type": "number"},
        }}}},
}

CONFIRM_PROMPT = """You review candidate groups of AI agents that different employees built independently. The goal is to replace each group of genuine duplicates with ONE shared agent on Langdock (base agent + small variants).

You get agents A1, A2, ... with their job, purpose, capabilities, systems, triggers and builder. Return the groups of agents that do the SAME recurring job for the business, even if built with different tools (Gmail vs Outlook, a Python script vs a chat app, scheduled vs on demand). An agent that does the same core job PLUS extra steps (also looks up or updates the CRM, also posts to chat, also drafts replies, writes the result to a file) IS a duplicate: put it in the group and describe the extra steps as its variant. Only leave an agent out if its core job is different. Only return groups with 2 or more members.

For each group:
- members: the agent refs (A1, ...).
- label: 2-5 words naming the shared job; reuse the clearest member name if it fits ("Customer email summarizer", "Weekly pipeline report").
- base_purpose: one concrete sentence the shared Langdock agent will do for everyone: input -> what it produces -> where it goes.
- shared_capabilities: 3-6 vendor-neutral capabilities every member needs.
- variants: one entry per member: what genuinely differs for that member, in a short phrase ("uses Outlook instead of Gmail", "adds CRM lookup in HubSpot", "runs on a schedule weekdays 07:30, AR focus"). Write "baseline" for the member closest to the base.
- rationale: one sentence why these are the same job.
- confidence: 0.0-1.0."""


JOIN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["joins"],
    "properties": {"joins": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["member", "belongs", "variant", "rationale"],
        "properties": {"member": _STR, "belongs": {"type": "boolean"}, "variant": _STR, "rationale": _STR}}}},
}

JOIN_PROMPT = """A confirmed group of duplicate AI agents (same recurring job, built by different employees) already exists. New agents were just discovered that look similar.

For each new agent (N1, N2, ...) decide whether it does the SAME core job as the group. An agent that does the same core job plus extra steps (CRM lookup, posting to chat, drafting replies, a schedule) belongs; describe what differs as its variant in a short phrase, e.g. "uses Outlook instead of Gmail, adds a CRM lookup". Only say it does not belong if its core job is different.

Return one entry per new agent: member (N-ref), belongs, variant, rationale (one sentence)."""


def _join_payload(group: dict, existing: list[dict], newcomers: list[dict]) -> tuple[str, dict[str, str]]:
    refs = {f"N{i + 1}": a["id"] for i, a in enumerate(newcomers)}

    def desc(a):
        return {"name": a["name"], "job": a.get("job"), "purpose": a["purpose"], "capabilities": a.get("capabilities"),
                "surface": a["surface"], "systems": [f"{r['system']} ({r['access']})" for r in a["data_reach"]],
                "triggers": [t["description"] for t in a["triggers"]]}

    payload = {"group": {k: group[k] for k in ("label", "base_purpose", "shared_capabilities")},
               "existing_members": [desc(a) for a in existing],
               "new_agents": [{"ref": r, **desc(a)} for r, a in zip(refs, newcomers)]}
    return "Existing group and new agents:\n" + json.dumps(scrub(payload), indent=1), refs


def _confirm_payload(group: list[dict]) -> tuple[str, dict[str, str]]:
    refs = {f"A{i + 1}": a["id"] for i, a in enumerate(group)}
    items = []
    for ref, a in zip(refs, group):
        items.append({"ref": ref, "name": a["name"], "job": a.get("job"), "purpose": a["purpose"],
                      "capabilities": a.get("capabilities"), "surface": a["surface"],
                      "systems": [f"{r['system']} ({r['access']})" for r in a["data_reach"]],
                      "triggers": [t["description"] for t in a["triggers"]],
                      "builder": {"department": a["owner"].get("department"), "title": a["owner"].get("title")}})
    return "Candidate agents:\n" + json.dumps(scrub(items), indent=1), refs


def membership_key(group: list[dict]) -> str:
    blob = json.dumps(sorted((a["id"], a["evidence_hash"], a.get("job")) for a in group))
    return hashlib.sha256(f"{blob}|{llm.PROVIDER}:{llm.MODEL}|{PROMPT_VERSION}".encode()).hexdigest()[:20]


class ClusterService:
    def __init__(self, on_change: Callable[[], None], cache_store: "ClusterCacheStore") -> None:
        self.embedder = Embedder()
        self.llm = llm.StructuredLLM()
        self.on_change = on_change
        self.store = cache_store
        self.confirmed: dict[str, dict] = cache_store.load()
        self.inflight: set[str] = set()
        self.failed: dict[str, float] = {}
        self.disabled_reason = None if llm.credentials_configured() else llm.missing_credentials_hint()
        self.stats = {"confirm_calls": 0, "errors": 0, "last_error": None, "last_ms": None}

    def start(self) -> None:
        loop = asyncio.get_running_loop()

        def bg():
            self.embedder.load()
            loop.call_soon_threadsafe(self.on_change)

        threading.Thread(target=bg, name="embedder", daemon=True).start()

    def status(self) -> dict:
        return {"embedder_ready": self.embedder.ready, "embedder_error": self.embedder.error,
                "llm_enabled": self.disabled_reason is None, "disabled_reason": self.disabled_reason,
                "threshold": CANDIDATE_THRESHOLD, "inflight": len(self.inflight), **self.stats}

    def compute(self, agents: dict[str, dict]) -> tuple[list[dict], dict]:
        """Clusters for the current agents. Requests LLM confirmation for unseen candidate groups."""
        groups, pairs = candidate_groups(list(agents.values()), self.embedder)
        clusters: list[dict] = []
        for ids in groups:
            members = sorted((agents[i] for i in ids), key=lambda a: a["first_seen"] or "")
            key = membership_key(members)
            result = self.confirmed.get(key)
            if result is not None:
                for g in result["groups"]:
                    mem = [agents[i] for i in g["member_ids"] if i in agents]
                    if len(mem) >= 2:
                        clusters.append(_confirmed_cluster(mem, g, pairs, result.get("_meta", {})))
                continue
            prior = self._prior_group(set(ids))
            if prior:  # an already-confirmed cluster + newcomers: only ask about the newcomers
                existing = [a for a in members if a["id"] in prior["member_ids"]]
                newcomers = [a for a in members if a["id"] not in prior["member_ids"]]
                self._request_join(key, prior, existing, newcomers)
                clusters.append(_joining_cluster(existing, newcomers, prior, pairs, pending=key in self.inflight))
                continue
            self._request(key, members)
            clusters.append(_unconfirmed_cluster(members, pairs, pending=key in self.inflight))
        return clusters, pairs

    def _prior_group(self, ids: set[str]) -> dict | None:
        """Largest confirmed group (2+ members) that is a strict subset of these candidate ids."""
        best = None
        for result in self.confirmed.values():
            for g in result.get("groups", []):
                mem = set(g["member_ids"])
                if len(mem) >= 2 and mem < ids and (best is None or len(mem) > len(best["member_ids"])):
                    best = g
        return best

    def _request_join(self, key: str, prior: dict, existing: list[dict], newcomers: list[dict]) -> None:
        if self.disabled_reason or key in self.inflight or time.monotonic() - self.failed.get(key, -1e9) < 60:
            return
        self.inflight.add(key)
        asyncio.create_task(self._join(key, prior, existing, newcomers))

    async def _join(self, key: str, prior: dict, existing: list[dict], newcomers: list[dict]) -> None:
        t0 = time.monotonic()
        try:
            user, refs = _join_payload(prior, existing, newcomers)
            out, usage = await self.llm.structured(JOIN_PROMPT, user, JOIN_SCHEMA, "cluster_join", max_tokens=1500)
            joined = {refs[j["member"]]: j for j in out["joins"] if j["member"] in refs and j["belongs"]}
            group = {**prior, "member_ids": sorted(set(prior["member_ids"]) | set(joined)),
                     "variants": {**prior["variants"], **{i: j["variant"] for i, j in joined.items()}}}
            ms = int((time.monotonic() - t0) * 1000)
            result = {"groups": scrub([group]), "_meta": {"model": f"{llm.PROVIDER}:{llm.MODEL}", "duration_ms": ms,
                                                           "created_at": now_iso(), "usage": usage, "mode": "join"}}
            self.confirmed[key] = result
            self.store.save(key, result)
            self.stats["confirm_calls"] += 1
            self.stats["last_ms"] = ms
            log.info("join decided in %d ms: %d of %d newcomers joined %s", ms, len(joined), len(newcomers), prior["label"])
        except (llm.LLMAuthError, llm.LLMModelError) as e:
            self.disabled_reason = str(e)
        except Exception as e:
            log.warning("cluster join failed: %s", e)
            self.failed[key] = time.monotonic()
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            self.inflight.discard(key)
            self.on_change()

    def _request(self, key: str, members: list[dict]) -> None:
        if self.disabled_reason or key in self.inflight or time.monotonic() - self.failed.get(key, -1e9) < 60:
            return
        self.inflight.add(key)
        asyncio.create_task(self._confirm(key, members))

    async def _confirm(self, key: str, members: list[dict]) -> None:
        t0 = time.monotonic()
        try:
            user, refs = _confirm_payload(members)
            out, usage = await self.llm.structured(CONFIRM_PROMPT, user, CONFIRM_SCHEMA, "cluster_review", max_tokens=3000)
            groups = []
            for g in out["groups"]:
                ids = [refs[m] for m in g["members"] if m in refs]
                if len(set(ids)) < 2:
                    continue
                variants = {refs[v["member"]]: v["diff"] for v in g["variants"] if v["member"] in refs}
                groups.append({**{k: g[k] for k in ("label", "base_purpose", "shared_capabilities", "rationale")},
                               "confidence": round(float(g["confidence"]), 2), "member_ids": sorted(set(ids)),
                               "variants": variants})
            ms = int((time.monotonic() - t0) * 1000)
            result = {"groups": scrub(groups), "_meta": {"model": f"{llm.PROVIDER}:{llm.MODEL}", "duration_ms": ms,
                                                          "created_at": now_iso(), "usage": usage}}
            self.confirmed[key] = result
            self.store.save(key, result)
            self.stats["confirm_calls"] += 1
            self.stats["last_ms"] = ms
            log.info("cluster confirmed in %d ms: %s", ms, [g["label"] for g in groups])
        except (llm.LLMAuthError, llm.LLMModelError) as e:
            self.disabled_reason = str(e)
        except Exception as e:
            log.warning("cluster confirmation failed: %s", e)
            self.failed[key] = time.monotonic()
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            self.inflight.discard(key)
            self.on_change()


def _systems(members: list[dict]) -> list[str]:
    return sorted({r["system"] for a in members for r in a["data_reach"]})


def _pair_scores(members: list[dict], pairs: dict) -> list[dict]:
    ids = [a["id"] for a in members]
    return [{"a": x, "b": y, **pairs[(x, y)]} for i, x in enumerate(ids) for y in ids[i + 1:] if (x, y) in pairs] + \
           [{"a": y, "b": x, **pairs[(y, x)]} for i, x in enumerate(ids) for y in ids[i + 1:] if (y, x) in pairs]


def _base(members: list[dict], pairs: dict) -> dict:
    anchor = members[0]  # earliest first_seen: stable id when new members join
    return {
        "id": "cl_" + anchor["id"].removeprefix("ag_"),
        "member_agent_ids": [a["id"] for a in members],
        "members": [{"agent_id": a["id"], "name": a["name"], "owner": a["owner"]["name"],
                     "department": a["owner"].get("department"), "surface": a["surface"],
                     "systems": [r["system"] for r in a["data_reach"]]} for a in members],
        "people": len({a["owner"]["id"] for a in members}),
        "systems": _systems(members),
        "pair_scores": _pair_scores(members, pairs),
        "risk_level": max((a["risk"]["level"] for a in members),
                          key=lambda l: {"none": 0, "low": 1, "medium": 2, "high": 3}[l]),
    }


def _confirmed_cluster(members: list[dict], g: dict, pairs: dict, meta: dict) -> dict:
    members = sorted(members, key=lambda a: a["first_seen"] or "")
    return {**_base(members, pairs), "status": "confirmed", "label": g["label"], "base_purpose": g["base_purpose"],
            "shared_capabilities": g["shared_capabilities"], "rationale": g["rationale"], "confidence": g["confidence"],
            "variants": [{"agent_id": a["id"], "diff": g["variants"].get(a["id"], "")} for a in members],
            "confirmation": meta}


def _joining_cluster(existing: list[dict], newcomers: list[dict], prior: dict, pairs: dict, pending: bool) -> dict:
    members = sorted(existing, key=lambda a: a["first_seen"] or "") + newcomers
    c = _confirmed_cluster(members, {**prior, "variants": {**prior["variants"]}}, pairs, {})
    c["status"] = "confirming" if pending else "candidate"
    c["joining_agent_ids"] = [a["id"] for a in newcomers]
    return c


def _unconfirmed_cluster(members: list[dict], pairs: dict, pending: bool) -> dict:
    job = Counter(a.get("job") for a in members).most_common(1)[0][0]
    return {**_base(members, pairs), "status": "confirming" if pending else "candidate",
            "label": (job or members[0]["name"]).capitalize(), "base_purpose": None,
            "shared_capabilities": sorted(set.intersection(*(set(a.get("capabilities") or []) for a in members))),
            "rationale": "Similar job, capabilities and systems (not yet confirmed by the LLM)", "confidence": None,
            "variants": [{"agent_id": a["id"], "diff": ""} for a in members], "confirmation": None}


class ClusterCacheStore:
    """SQLite persistence for confirmations (same DB as signals; survives demo resets)."""

    def __init__(self) -> None:
        from .db import ClusterCache  # noqa: F401  (registers table)

    def load(self) -> dict[str, dict]:
        from .db import ClusterCache, session

        with session() as s:
            return {r.key: r.result for r in s.query(ClusterCache).all()}

    def save(self, key: str, result: dict) -> None:
        from .db import ClusterCache, session

        with session() as s:
            s.merge(ClusterCache(key=key, result=result, created_at=now_iso()))
            s.commit()
