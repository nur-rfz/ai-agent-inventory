# Fixtures: the simulated org (Nordlicht GmbH)

Real org-wide sensors are out of scope, so this folder simulates what sensors on six
colleagues' laptops plus three identity/vendor log exports would report.

## Who built what
| Person | Dept | Agent | Built with | Story |
|---|---|---|---|---|
| Maya Patel | Support | summarize customer emails | Claude Desktop + Gmail MCP | personal Google OAuth client with `gmail.modify` |
| Jonas Weber | Sales | summarize customer emails **+ CRM lookup** | Claude Desktop + Outlook MCP (ms-365) + HubSpot MCP | user consent to `Mail.ReadWrite Mail.Send` |
| Priya Raman | Finance | summarize customer emails (AR focus) | Python cron 07:30 Mon–Fri, OpenAI + Microsoft Graph | app reg with **application** `Mail.Read` (tenant-wide), new OpenAI key on service account |
| Lena Hoffmann | RevOps | weekly pipeline report | Python cron Mon 08:00, Salesforce + Claude + Slack | also: ChatGPT consent to SharePoint files |
| Tom Becker | Sales | weekly pipeline report (on demand, Mon/Thu) | Claude Desktop + Salesforce MCP + Slack MCP | Salesforce toolset can write records; Fireflies notetaker consent |
| Alex Kim | Engineering | coding agents | Cursor (GitHub remote MCP, Postgres MCP `--access-mode=unrestricted` on billing DB) + Claude Code (Sentry, Linear) | govern in place |
| IT Admin | IT | Langdock | admin consent | sanctioned |

The live demo agent (`scripts/demo_mcp_server.py`, Gmail + HubSpot + Slack) is designed to
join the "summarize customer emails" group as a 4th variant.

## Files
- `colleagues/<id>/`: their real artifacts: MCP config files, scripts, `.env` (placeholder
  values), crontab. Secrets in here are obviously fake and are redacted anyway.
- `probes.yaml`: what `tools/list` returned on their machines (allowlisted stdio
  servers were probed; `postgres-mcp` was not; Sentry/Linear need OAuth, so probes fail).
- `identity/`: vendor-shaped exports: Entra `directoryAudits`, Google Workspace Reports
  `token` activity, OpenAI Admin `audit_logs`. Normalized by `backend/identity.py`.
- `directory.yaml`: who is behind which laptop user / email (for owner resolution).
- `policy.yaml`: sanctioned apps, approved LLM providers, systems of record.
- `build.py` → `signals.jsonl`: runs the **real sensor code** (parsers, codescan, crontab
  parser, process records, network attribution) over `colleagues/`. Rebuild after
  editing anything here: `python fixtures/build.py` (a test checks the file is current).

## Timeline
Sensors rolled out Mon 2026-10-26; history through Fri 2026-11-13. `scripts/seed.py`
shifts all timestamps by whole weeks so the newest event is < 7 days old on demo day.
