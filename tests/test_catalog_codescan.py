from pathlib import Path

from common.catalog import get_catalog
from sensor.codescan import scan_folder
from sensor.netattr import NetAttributor


def test_catalog_exact_wildcard_and_port():
    c = get_catalog()
    assert c.lookup_host("api.openai.com").name == "OpenAI"
    assert c.lookup_host("acme.atlassian.net").name == "Jira"
    assert c.lookup_host("eu1.my.salesforce.com").name == "Salesforce"
    assert c.lookup_host("unknown.example") is None
    assert c.lookup_port(5432).name == "Postgres"


def test_attribution_uses_resolved_ip_map_then_port():
    n = NetAttributor(get_catalog())
    n.ip_map["104.18.0.1"] = ("api.openai.com", "OpenAI")
    assert n.attribute("104.18.0.1", 443)["system"] == "OpenAI"
    pg = n.attribute("10.0.0.5", 5432)
    assert pg["system"] == "Postgres" and pg["private"] is True


def test_codescan_names_only(tmp_path: Path):
    (tmp_path / "bot.py").write_text("import os\nfrom openai import OpenAI\nimport msal\nk=os.environ['OPENAI_API_KEY']\n")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=" + "sk-" + "proj-shouldneverappear1234567890\nDEBUG=1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("import OpenAI from 'openai'")
    res = scan_folder(tmp_path)
    blob = str(res)
    assert "shouldneverappear" not in blob
    bot = next(r for r in res if r["relative_path"] == "bot.py")
    assert bot["sdks"] == ["openai"] and "Microsoft 365" in bot["systems"]
    assert "OPENAI_API_KEY" in bot["env_names"]
    env = next(r for r in res if r.get("env_file"))
    assert env["env_names"] == ["OPENAI_API_KEY"]
    assert not any("node_modules" in r["relative_path"] for r in res)
