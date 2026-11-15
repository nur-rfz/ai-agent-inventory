"""Weekly pipeline report for #sales-leadership.

Pulls open opportunities from Salesforce, has Claude write the narrative, posts to Slack.
Scheduled Mondays 08:00 via cron.
"""
import os
from datetime import date, timedelta

import anthropic
from simple_salesforce import Salesforce
from slack_sdk import WebClient

sf = Salesforce(username=os.environ["SF_USERNAME"], password=os.environ["SF_PASSWORD"],
                security_token=os.environ["SF_SECURITY_TOKEN"], domain="login")
slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
claude = anthropic.Anthropic()

REPORT_PROMPT = """Write the weekly pipeline report for the Nordlicht sales leadership channel.
Sections: 1) pipeline total and change vs last week, 2) new opportunities > 20k EUR,
3) stage changes, 4) deals whose close date slipped, 5) top 5 deals at risk with a one-line reason.
Use short bullets, EUR, and name the opportunity owner."""


def opportunities():
    since = (date.today() - timedelta(days=7)).isoformat()
    q = ("SELECT Id, Name, Amount, StageName, CloseDate, Owner.Name, LastModifiedDate "
         f"FROM Opportunity WHERE IsClosed = false AND LastModifiedDate >= {since}T00:00:00Z")
    return sf.query_all(q)["records"]


if __name__ == "__main__":
    opps = opportunities()
    msg = claude.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        max_tokens=1500,
        system=REPORT_PROMPT,
        messages=[{"role": "user", "content": str(opps)}],
    )
    slack.chat_postMessage(channel="#sales-leadership", text=msg.content[0].text)
