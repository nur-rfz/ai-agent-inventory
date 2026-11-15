"""Secret redaction shared by the sensor and the backend.

Rule of thumb: names are kept, values are dropped. Anything that leaves the
sensor goes through `scrub()` as a last line of defence, even if the caller
already redacted its fields.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "<redacted>"

# Key / flag names that indicate the value is a credential.
SENSITIVE_NAME = re.compile(
    r"(api[_-]?key|apikey|token|secret|passw(or)?d|passwd|pwd|auth|credential|"
    r"private[_-]?key|access[_-]?key|session|cookie|bearer|signature|sig|dsn|"
    r"conn(ection)?[_-]?str(ing)?|client[_-]?secret|webhook)",
    re.IGNORECASE,
)

# Query params that carry credentials under short names (?key=..., ?code=...).
BARE_KEY_PARAMS = {"key", "k", "code", "access", "pass", "pw", "x-api-key", "apikey", "sv", "se", "sp"}

# Well-known credential shapes. Order matters only for readability.
SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),                 # Anthropic
    re.compile(r"sk-(proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{16,}"),  # OpenAI
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),                  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_\-]{16,}"),                   # GitLab
    re.compile(r"xox[abposr]-[A-Za-z0-9\-]{10,}"),              # Slack
    re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+"),
    re.compile(r"(AKIA|ASIA)[A-Z0-9]{16}"),                      # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                      # Google API key
    re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}"),                    # Google OAuth access token
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"(sk|rk|pk)_(live|test)_[A-Za-z0-9]{16,}"),      # Stripe
    re.compile(r"ATATT[A-Za-z0-9_\-=]{20,}"),                    # Atlassian API token
    re.compile(r"ntn_[A-Za-z0-9]{20,}|secret_[A-Za-z0-9]{30,}"),  # Notion
    re.compile(r"lin_api_[A-Za-z0-9]{20,}"),                     # Linear
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9_\-\.=+/]{12,}"),
]

# Generic high-entropy token: long, no path separators, mixes letters and digits.
GENERIC_TOKEN = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])")
# user:password@ in any URL-ish string
URL_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)([^/@\s:]+):([^/@\s]+)@")


def _entropy(tok: str) -> float:
    from collections import Counter
    from math import log2

    n = len(tok)
    return -sum(c / n * log2(c / n) for c in Counter(tok).values())


_HEX = re.compile(r"[0-9a-fA-F]{32,}")


def _is_generic_secret(tok: str) -> bool:
    """Long random-looking token.

    - 32+ hex chars: secret (hex API keys, signing secrets).
    - Mostly readable word segments (`summarize_customer_emails_v2_final`,
      `Directory_5b2c-ms365-mcp-user-consent`): not a secret.
    - Otherwise: several digits + letters + high entropy.
    """
    if _HEX.fullmatch(tok):
        return True
    words = [seg for seg in re.split(r"[_\-]", tok) if seg.isalpha() and len(seg) >= 3]
    if len(words) >= 2 and sum(map(len, words)) / len(tok) >= 0.5:
        return False
    digits = sum(c.isdigit() for c in tok)
    alpha = sum(c.isalpha() for c in tok)
    return digits >= 3 and alpha >= 3 and _entropy(tok) >= 3.5


def redact_text(text: str) -> str:
    """Redact credential-looking substrings from free text."""
    if not text:
        return text
    out = URL_USERINFO.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", text)
    for pat in SECRET_PATTERNS:
        out = pat.sub(REDACTED, out)
    out = GENERIC_TOKEN.sub(lambda m: REDACTED if _is_generic_secret(m.group(0)) else m.group(0), out)
    return out


def redact_url(url: str) -> str:
    """Drop passwords in userinfo and values of sensitive query params."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_text(url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, hostport = netloc.rpartition("@")
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:{REDACTED}@{hostport}" if ":" in userinfo else f"{REDACTED}@{hostport}"
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        pairs = [(k, REDACTED if (SENSITIVE_NAME.search(k) or k.lower() in BARE_KEY_PARAMS) else redact_text(v))
                 for k, v in pairs]
        query = urlencode(pairs, safe="<>")
    path = redact_text(parts.path)
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def redact_args(args: list[str]) -> list[str]:
    """Redact CLI args: `--token X`, `--token=X`, `KEY=value`, URLs and token shapes."""
    out: list[str] = []
    redact_next = False
    for a in args:
        a = str(a)
        if redact_next:
            out.append(REDACTED)
            redact_next = False
            continue
        if a.startswith("-") and "=" in a:
            flag, _, val = a.partition("=")
            out.append(f"{flag}={REDACTED}" if SENSITIVE_NAME.search(flag) else f"{flag}={_redact_value(val)}")
            continue
        if a.startswith("-") and SENSITIVE_NAME.search(a):
            out.append(a)
            redact_next = True
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", a)
        if m:  # env-style KEY=value passed as an arg (e.g. docker -e)
            out.append(f"{m.group(1)}={REDACTED}")
            continue
        out.append(_redact_value(a))
    return out


def _redact_value(v: str) -> str:
    if "://" in v:
        return redact_url(v)
    return redact_text(v)


def redact_mapping_values(d: dict[str, Any] | None) -> dict[str, str]:
    """Keep keys, drop every value (env vars, headers)."""
    return {str(k): REDACTED for k in (d or {})}


# Structural identifiers we generate ourselves (uuids, sha256 fingerprints).
SAFE_KEYS = {"id", "fingerprint", "server_fingerprint", "signal_id", "evidence_hash", "ts"}
_SAFE_SHAPE = re.compile(r"[0-9a-fA-F\-:.TZ+]{8,72}")
# Vendor object identifiers (app ids, audit record ids). Public identifiers, not credentials,
# and needed to link events; a looser shape, still no whitespace or long base64 blobs.
ID_KEYS = {"app_id", "client_id", "record_id", "object_id", "project_id", "tenant_id", "principal_id"}
_ID_SHAPE = re.compile(r"[A-Za-z0-9_\-:.]{3,96}")


def _is_structural(key: str | None, v: str) -> bool:
    return (key in SAFE_KEYS and bool(_SAFE_SHAPE.fullmatch(v))) or (key in ID_KEYS and bool(_ID_SHAPE.fullmatch(v)))


def scrub(obj: Any) -> Any:
    """Deep-walk any JSON-like object and redact secrets in every string.

    String values under sensitive-looking keys are replaced wholesale.
    """
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            if isinstance(v, str) and _is_structural(k, v):
                res[k] = v
            elif isinstance(k, str) and isinstance(v, str) and SENSITIVE_NAME.search(k) and v != REDACTED:
                res[k] = REDACTED
            else:
                res[k] = scrub(v)
        return res
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, tuple):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        return _redact_value(obj) if "://" in obj else redact_text(obj)
    return obj


def find_secrets(text: str) -> list[str]:
    """Return credential-looking substrings (used by tests and the emitter guard)."""
    hits: list[str] = []
    for pat in SECRET_PATTERNS:
        hits += [m.group(0) for m in pat.finditer(text)]
    hits += [m.group(0) for m in URL_USERINFO.finditer(text) if m.group(3) != REDACTED]
    hits += [m.group(0) for m in GENERIC_TOKEN.finditer(text) if _is_generic_secret(m.group(0))]
    return hits


def leaks(obj: Any, _key: str | None = None) -> list[str]:
    """Secret-shaped strings anywhere in a JSON-like object (structural ids excluded)."""
    if isinstance(obj, dict):
        return [h for k, v in obj.items() for h in leaks(v, k)]
    if isinstance(obj, (list, tuple)):
        return [h for v in obj for h in leaks(v, _key)]
    if isinstance(obj, str):
        if _is_structural(_key, obj):
            return []
        return [h for h in find_secrets(obj) if h != REDACTED]
    return []
