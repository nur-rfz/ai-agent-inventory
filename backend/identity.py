"""Normalize identity-provider / AI-vendor audit exports into signals.

Inputs are the vendors' own shapes, so the same code runs on real exports:
- Microsoft Entra: Graph `auditLogs/directoryAudits` (ApplicationManagement)
- Google Workspace: Admin SDK Reports `activities` for applicationName=token
- OpenAI: Admin API `organization/audit_logs`
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from common.schema import SignalIn, fingerprint

ZERO = timedelta(0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _unquote(v: Any) -> str:
    return str(v or "").strip().strip('"')


def _sig(source: str, kind: str, host: str, user: str, ts: datetime, raw: dict, fp: str, rid: str) -> SignalIn:
    import uuid

    return SignalIn(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{rid}")), ts=_iso(ts), source=source,
                    host=host, user=user, kind=kind, raw=raw, fingerprint=fp)


# --- Entra -------------------------------------------------------------------------

_SCOPE = re.compile(r"Scope:\s+([^,\]]+)")


def entra(doc: dict, shift: timedelta = ZERO, tenant: str = "entra") -> list[SignalIn]:
    out = []
    for rec in doc.get("value", []):
        act = rec.get("activityDisplayName", "")
        who = ((rec.get("initiatedBy") or {}).get("user") or {}).get("userPrincipalName") or "unknown"
        tgt = (rec.get("targetResources") or [{}])[0]
        props = {p.get("displayName"): _unquote(p.get("newValue")) for p in tgt.get("modifiedProperties") or []}
        details = {d.get("key"): d.get("value") for d in rec.get("additionalDetails") or []}
        app_id = details.get("AppId") or props.get("TargetId.ServicePrincipalNames") or props.get("ServicePrincipal.AppId")
        app_name = tgt.get("displayName")
        raw: dict[str, Any] = {"provider": "entra", "activity": act, "user_email": who, "app_id": app_id,
                               "record_id": rec.get("id"), "result": rec.get("result")}
        if act == "Consent to application":
            scopes = []
            for m in _SCOPE.finditer(props.get("ConsentAction.Permissions", "")):
                scopes += m.group(1).split()
            admin = props.get("ConsentContext.IsAdminConsent") == "True"
            raw |= {"event": "consent", "app_name": app_name, "scopes": scopes, "permission_type": "delegated",
                    "consent_type": "admin" if admin else "user",
                    "on_behalf_of_all": props.get("ConsentContext.OnBehalfOfAll") == "True", "resource": "Microsoft Graph"}
        elif act == "Add app role assignment to service principal":
            raw |= {"event": "app_role_assignment", "app_name": props.get("ServicePrincipal.DisplayName") or app_name,
                    "scopes": [props.get("AppRole.Value")] if props.get("AppRole.Value") else [],
                    "permission_type": "application", "consent_type": "admin", "resource": app_name}
        elif act == "Add application":
            raw |= {"event": "app_registered", "app_name": app_name, "registered_by": who}
        elif "Certificates and secrets" in act:
            raw |= {"event": "credential_added", "app_name": app_name,
                    "credential": "client_secret" if "Password" in props.get("KeyDescription", "") else "certificate"}
        else:
            continue
        ts = _parse(rec["activityDateTime"]) + shift
        raw["occurred_at"] = _iso(ts)
        out.append(_sig("connector:entra", "oauth_grant", tenant, who, ts, raw,
                        fingerprint("oauth_app", "entra", app_id or app_name), rec["id"]))
    return out


# --- Google Workspace ------------------------------------------------------------

def google_workspace(doc: dict, shift: timedelta = ZERO, tenant: str = "google_workspace") -> list[SignalIn]:
    out = []
    for item in doc.get("items", []):
        who = (item.get("actor") or {}).get("email") or "unknown"
        for i, ev in enumerate(item.get("events") or []):
            params = {}
            for p in ev.get("parameters") or []:
                params[p["name"]] = p.get("multiValue") or p.get("value") or p.get("intValue")
            name = ev.get("name")
            raw: dict[str, Any] = {"provider": "google_workspace", "user_email": who, "app_id": params.get("client_id"),
                                   "app_name": params.get("app_name"), "client_type": params.get("client_type"),
                                   "record_id": item["id"]["uniqueQualifier"]}
            if name == "authorize":
                raw |= {"event": "consent", "consent_type": "user", "permission_type": "delegated",
                        "scopes": params.get("scope") or [], "resource": "Google Workspace"}
            elif name == "request":
                raw |= {"event": "api_access", "api_name": params.get("api_name"), "method": params.get("method_name"),
                        "response_bytes": int(params.get("num_response_bytes") or 0)}
            elif name == "revoke":
                raw |= {"event": "revoked"}
            else:
                continue
            ts = _parse(item["id"]["time"]) + shift
            raw["occurred_at"] = _iso(ts)
            out.append(_sig("connector:google_workspace", "oauth_grant", tenant, who, ts, raw,
                            fingerprint("oauth_app", "google", params.get("client_id")), f"{item['id']['uniqueQualifier']}:{i}"))
    return out


# --- OpenAI admin audit logs -------------------------------------------------------

def openai_audit(doc: dict, shift: timedelta = ZERO, org: str = "openai") -> list[SignalIn]:
    out = []
    for rec in doc.get("data", []):
        typ = rec.get("type", "")
        if typ not in ("api_key.created", "api_key.deleted", "service_account.created", "project.created"):
            continue
        who = (((rec.get("actor") or {}).get("session") or {}).get("user") or {}).get("email") or "unknown"
        proj = rec.get("project") or {}
        body = rec.get(typ) or {}
        data = body.get("data") or {}
        raw: dict[str, Any] = {"provider": "openai", "event": typ, "user_email": who, "project_id": proj.get("id"),
                               "project_name": proj.get("name"), "object_id": body.get("id"), "record_id": rec["id"]}
        if typ.startswith("api_key"):
            raw |= {"scopes": data.get("scopes"), "owner_type": (data.get("owner") or {}).get("type", "user"),
                    "owner_name": (data.get("owner") or {}).get("name"), "key_hint": data.get("redacted_value")}
        if typ == "service_account.created":
            raw |= {"service_account": data.get("name"), "role": data.get("role")}
        ts = datetime.fromtimestamp(rec["effective_at"], timezone.utc) + shift
        raw["occurred_at"] = _iso(ts)
        out.append(_sig("connector:openai_admin", "api_key", org, who, ts, raw,
                        fingerprint("llm_project", "openai", proj.get("id")), rec["id"]))
    return out


NORMALIZERS = {"entra": entra, "google_workspace": google_workspace, "openai": openai_audit}


def normalize(provider: str, doc: dict, shift: timedelta = ZERO) -> list[SignalIn]:
    return NORMALIZERS[provider](doc, shift)


def latest_time(provider: str, doc: dict) -> datetime:
    sigs: Iterable[SignalIn] = normalize(provider, doc)
    return max(_parse(s.ts) for s in sigs)
