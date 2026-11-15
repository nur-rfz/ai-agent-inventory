"""Summarize new customer emails in the AR shared mailbox every weekday morning.

Reads unread mail from ar@nordlicht.example via Microsoft Graph, asks GPT for a short
summary per thread, and writes the digest to ar_digest.xlsx on the Finance SharePoint.
Runs from cron at 07:30 (Mon-Fri).
"""
import os

import msal
import requests
from openai import OpenAI
from openpyxl import Workbook

GRAPH = "https://graph.microsoft.com/v1.0"
MAILBOX = "ar@nordlicht.example"
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

SYSTEM_PROMPT = """You are an assistant for the accounts receivable team at Nordlicht.
Summarize each customer email thread in 3 bullets: who the customer is, what they are
asking about (invoice copy, payment status, dispute, dunning reply, remittance advice),
and the suggested next step for AR. Flag anything that mentions a dispute or legal action."""


def token():
    app = msal.ConfidentialClientApplication(
        os.environ["AZURE_CLIENT_ID"],
        authority=f"https://login.microsoftonline.com/{os.environ['AZURE_TENANT_ID']}",
        client_credential=os.environ["AZURE_CLIENT_SECRET"],
    )
    return app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])["access_token"]


def unread(tok):
    r = requests.get(f"{GRAPH}/users/{MAILBOX}/mailFolders/inbox/messages",
                     params={"$filter": "isRead eq false", "$top": 50},
                     headers={"Authorization": f"Bearer {tok}"})
    return r.json()["value"]


def summarize(msg):
    out = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": msg["subject"] + "\n\n" + msg["bodyPreview"]}],
    )
    return out.choices[0].message.content


if __name__ == "__main__":
    tok = token()
    wb = Workbook()
    ws = wb.active
    ws.append(["From", "Subject", "Summary"])
    for m in unread(tok):
        ws.append([m["from"]["emailAddress"]["address"], m["subject"], summarize(m)])
    wb.save(os.path.expanduser("~/Library/CloudStorage/OneDrive-Nordlicht/Finance/ar_digest.xlsx"))
