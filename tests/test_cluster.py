import numpy as np

from backend.cluster import _confirmed_cluster, _unconfirmed_cluster, candidate_groups, categories


class FakeEmbedder:
    """Deterministic vectors: same job text -> same direction."""
    ready = True

    def __init__(self, vecs):
        self.vecs = vecs

    def encode(self, texts):
        return np.stack([self.vecs[t.split(".")[0]] for t in texts])


def agent(aid, job, systems, caps, first_seen, cls="replace", owner="p"):
    return {"id": aid, "name": job.title(), "job": job, "capabilities": caps, "classification": cls,
            "first_seen": first_seen, "evidence_hash": aid, "surface": "script", "purpose": job,
            "data_reach": [{"system": s, "access": "r"} for s in systems], "triggers": [],
            "owner": {"id": owner, "name": owner, "department": None, "title": None}, "risk": {"level": "low"}}


V = {"summarize customer emails": np.array([1.0, 0.0, 0.0]),
     "triage support inbox": np.array([0.8, 0.6, 0.0]),
     "weekly sales pipeline report": np.array([0.0, 0.0, 1.0]),
     "review pull requests": np.array([0.0, 1.0, 0.0])}


def test_categories_treat_gmail_and_outlook_mail_alike():
    gmail = agent("a", "summarize customer emails", ["Gmail"], ["read inbox"], "1")
    outlook = agent("b", "summarize customer emails", ["Microsoft 365"], ["read unread emails"], "2")
    files = agent("c", "answer questions", ["Microsoft 365"], ["read sharepoint documents"], "3")
    assert categories(gmail) == categories(outlook) == {"email"}
    assert categories(files) == {"files"}


def test_candidate_groups_use_embedding_plus_category_overlap():
    ags = [agent("a1", "summarize customer emails", ["Gmail"], ["read inbox"], "1", owner="maya"),
           agent("a2", "summarize customer emails", ["Microsoft 365", "HubSpot"], ["read email", "crm lookup"], "2", owner="jonas"),
           agent("a3", "triage support inbox", ["Gmail", "Slack"], ["read inbox", "post to slack"], "3", owner="you"),
           agent("b1", "weekly sales pipeline report", ["Salesforce", "Slack"], ["post report to slack"], "4", owner="lena"),
           agent("c1", "review pull requests", ["GitHub"], ["review code"], "5", cls="govern_in_place")]
    groups, pairs = candidate_groups(ags, FakeEmbedder(V))
    assert groups == [["a1", "a2", "a3"]]  # coding agent excluded, pipeline report alone
    assert pairs[("a1", "a2")]["score"] > pairs[("a1", "b1")]["score"]


def test_cluster_id_is_anchored_on_earliest_member():
    a1 = agent("ag_first", "summarize customer emails", ["Gmail"], ["x"], "2026-10-01")
    a2 = agent("ag_second", "summarize customer emails", ["Gmail"], ["x"], "2026-10-02")
    live = agent("ag_live", "summarize customer emails", ["Gmail"], ["x"], "2026-11-17")
    g = {"label": "L", "base_purpose": "b", "shared_capabilities": [], "rationale": "r", "confidence": 0.9,
         "variants": {}, "member_ids": []}
    before = _confirmed_cluster([a2, a1], g, {}, {})
    after = _unconfirmed_cluster([a1, a2, live], {}, pending=True)
    assert before["id"] == after["id"] == "cl_first"
    assert after["status"] == "confirming" and len(after["member_agent_ids"]) == 3
