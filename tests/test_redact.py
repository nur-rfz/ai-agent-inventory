import json

import pytest

from common.redact import REDACTED, leaks, redact_args, redact_text, redact_url, scrub
from sensor.parsers import RawServer, parse_data
from sensor.paths import ConfigLocation
from pathlib import Path

# Fake tokens, assembled at runtime so no literal credential shape sits in the source
# (GitHub push protection flags those even when they are obviously fake).
def _t(*parts: str) -> str:
    return "".join(parts)


SECRETS = [
    _t("sk-", "proj-", "AbCdEf1234567890GhIjKlMnOp"),
    _t("sk-", "ant-", "api03-ZZZZ1234567890abcdefghij"),
    _t("gh", "p_", "abcdefghijklmnopqrstuvwxyz0123456789"),
    _t("github", "_pat_", "11ABCDEFG0123456789_abcdefghijklmnop"),
    _t("xo", "xb-", "123456789012-1234567890123-AbCdEfGhIjKl"),
    _t("AK", "IA", "IOSFODNN7EXAMPLE"),
    _t("AI", "za", "SyA1234567890abcdefghijklmnopqrstu"),
    _t("eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
    _t("ATA", "TT", "3xFfGF0abcdefghijklmnopqrstuvwxyz123456"),
    _t("sk", "_live_", "51H8abcdefghijklmnopqrstuv"),
    _t("lin", "_api_", "abcdefghijklmnopqrstuvwxyz1234"),
    "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c",  # generic 40-char hex token
]


@pytest.mark.parametrize("secret", SECRETS)
def test_known_token_shapes_are_redacted(secret):
    assert secret not in redact_text(f"value: {secret} end")
    assert secret not in json.dumps(scrub({"note": f"x {secret}"}))


def test_bearer_header_text():
    assert "abc123def456ghi789" not in redact_text("Authorization: Bearer abc123def456ghi789")


def test_url_userinfo_and_query():
    u = redact_url("postgresql://admin:hunter2pass@db.internal:5432/crm?sslmode=require&password=x9y8z7")
    assert "hunter2pass" not in u and "x9y8z7" not in u
    assert "db.internal:5432" in u and "admin" in u and "sslmode=require" in u
    u2 = redact_url("https://api.example.com/mcp?api_key=SECRETVALUE123&team=sales")
    assert "SECRETVALUE123" not in u2 and "team=sales" in u2


def test_args_flags_and_env_style():
    args = ["-y", "@modelcontextprotocol/server-postgres", "--api-key", "plainpassword",
            "--token=tok_value_1", "-e", "GITHUB_TOKEN=whatever", "postgres://u:pw123@h/db",
            "/Users/alice/projects/summarize_customer_emails_v2_final.py"]
    out = redact_args(args)
    s = " ".join(out)
    for bad in ("plainpassword", "tok_value_1", "whatever", "pw123"):
        assert bad not in s
    # non-secrets survive
    assert "@modelcontextprotocol/server-postgres" in out
    assert "--api-key" in out
    assert "GITHUB_TOKEN=" + REDACTED in out
    assert out[-1].endswith("summarize_customer_emails_v2_final.py")


def test_scrub_sensitive_keys_and_keeps_structure():
    data = {"password": "p@ss w0rd!", "client_secret": "short", "port": 5432, "enabled": True,
            "server_fingerprint": "a" * 64, "nested": [{"token": "abc"}]}
    out = scrub(data)
    assert out["password"] == REDACTED and out["client_secret"] == REDACTED
    assert out["nested"][0]["token"] == REDACTED
    assert out["port"] == 5432 and out["enabled"] is True
    assert out["server_fingerprint"] == "a" * 64  # our own ids stay intact
    assert leaks(out) == []


def test_parsed_server_public_view_has_no_secret_values():
    cfg = {"mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRETS[2]}},
        "crm": {"url": "https://mcp.acme.example/mcp?key=zzz123zzz", "headers": {"Authorization": "Bearer s3cr3t-value"}},
        "pg": {"command": "uvx", "args": ["postgres-mcp", "postgresql://app:Sup3rS3cret@10.0.0.5:5432/prod"],
               "env": {"JIRA_URL": "https://acme.atlassian.net", "SOME_VAL": "not-obviously-secret"}},
    }}
    loc = ConfigLocation("cursor", Path("/home/u/.cursor/mcp.json"), "mcpServers", "user")
    servers = parse_data(cfg, loc)
    blob = json.dumps([scrub(s.public()) for s in servers])
    for bad in ("ghp_abcdefghij", "s3cr3t-value", "zzz123zzz", "Sup3rS3cret", "not-obviously-secret"):
        assert bad not in blob, bad
    # names and hosts are kept
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in blob and "Authorization" in blob
    pg = next(s for s in servers if s.name == "pg")
    assert "acme.atlassian.net" in pg.public()["hostnames"]
    # fingerprints are derived from redacted data: identical across secret rotation
    pg2 = RawServer(**{**pg.__dict__, "args": ["postgres-mcp", "postgresql://app:Other@10.0.0.5:5432/prod"]})
    assert pg.fingerprint == pg2.fingerprint


def test_vendor_ids_survive_but_secrets_under_id_keys_with_spaces_do_not():
    raw = {"app_id": "084a3e9f-a9f4-43f7-89f9-d229cf97853e", "record_id": "Directory_5b2c-ms365-mcp-user-consent",
           "client_secret": "084a3e9f-a9f4-43f7-89f9-d229cf97853e", "note": "app 084a3e9fa9f443f789f9d229cf97853e"}
    out = scrub(raw)
    assert out["app_id"] == raw["app_id"] and out["record_id"] == raw["record_id"]
    assert out["client_secret"] == REDACTED          # sensitive key name wins
    assert "084a3e9fa9f443f789f9d229cf97853e" not in out["note"]  # free text still scrubbed
    assert leaks(out) == []
