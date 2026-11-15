"""The MCP server you add in Cursor on camera: a small "customer inbox digest" server.

Runs locally over Streamable HTTP so the sensor can probe it without spawning
anything:  python scripts/demo_mcp_server.py  ->  http://127.0.0.1:8931/mcp

Add to ~/.cursor/mcp.json:
  "inbox-digest": {"url": "http://127.0.0.1:8931/mcp", "headers": {"Authorization": "Bearer <token>"}}

Tool bodies return canned data; only the shape matters for discovery.
"""

from __future__ import annotations

import argparse

from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="inbox-digest",
    version="0.3.1",
    instructions=(
        "Triage the customer support inbox in Gmail: find new customer emails, summarize each "
        "thread, look up the account in HubSpot, and draft a reply. Posts a daily digest to "
        "Slack #support-digest at 08:00."
    ),
)


@server.tool()
def search_customer_emails(query: str = "is:unread label:customers", max_results: int = 20) -> list[dict]:
    """Search the shared Gmail support inbox for customer emails matching a Gmail query."""
    return [{"thread_id": "t-1", "from": "ops@customer.example", "subject": "Invoice question"}]


@server.tool()
def get_email_thread(thread_id: str) -> dict:
    """Fetch every message in a Gmail thread (sender, date, body text)."""
    return {"thread_id": thread_id, "messages": []}


@server.tool()
def summarize_thread(thread_id: str, style: str = "bullets") -> str:
    """Summarize a customer email thread: ask, sentiment, urgency, next step."""
    return "- Customer asks about a duplicate invoice\n- Urgency: medium"


@server.tool()
def lookup_account(email: str) -> dict:
    """Look up the sender's company, plan and owner in HubSpot CRM."""
    return {"company": "Customer Inc", "plan": "Enterprise", "owner": "sam@yourco.example"}


@server.tool()
def draft_reply(thread_id: str, tone: str = "friendly") -> str:
    """Create a Gmail draft reply to the thread (does not send)."""
    return "draft-123"


@server.tool()
def post_digest(channel: str = "#support-digest") -> str:
    """Post the daily inbox digest to a Slack channel."""
    return "ok"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8931)
    a = ap.parse_args()
    server.run("streamable-http", host="127.0.0.1", port=a.port)
