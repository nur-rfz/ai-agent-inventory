from pathlib import Path

from sensor.parsers import loads_jsonc, parse_data
from sensor.paths import ConfigLocation, current_os, user_locations
from sensor.process_watcher import match_server, surface_from_chain


def loc(fmt, client="cursor", scope="user", path="/h/.cursor/mcp.json"):
    return ConfigLocation(client, Path(path), fmt, scope)


def test_jsonc_comments_and_trailing_commas():
    text = '{\n // comment\n "a": "http://x//y", /* block */ "b": [1,2,],\n}'
    assert loads_jsonc(text) == {"a": "http://x//y", "b": [1, 2]}


def test_cursor_stdio_and_http():
    servers = parse_data({"mcpServers": {
        "gh": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
        "remote": {"url": "https://mcp.linear.app/sse"},
        "broken": {"args": ["no command"]},
    }}, loc("mcpServers"))
    by = {s.name: s for s in servers}
    assert set(by) == {"gh", "remote"}
    assert by["gh"].transport == "stdio" and by["remote"].transport == "sse"


def test_vscode_mcp_json_and_settings():
    vs = parse_data({"servers": {"pg": {"type": "stdio", "command": "uvx", "args": ["pg-mcp"]},
                                 "web": {"type": "http", "url": "https://x/mcp"}}, "inputs": []},
                    loc("vscode_servers", "vscode"))
    assert {s.name for s in vs} == {"pg", "web"}
    st = parse_data({"editor.fontSize": 12, "mcp": {"servers": {"a": {"command": "node", "args": ["a.js"]}}}},
                    loc("vscode_settings", "vscode"))
    assert [s.name for s in st] == ["a"]


def test_windsurf_server_url():
    s = parse_data({"mcpServers": {"r": {"serverUrl": "https://r.example/mcp"}}}, loc("mcpServers", "windsurf"))
    assert s[0].transport == "http" and s[0].url == "https://r.example/mcp"


def test_claude_code_user_and_local_scopes():
    data = {"mcpServers": {"u": {"command": "npx", "args": ["x"]}},
            "projects": {"/repo/app": {"mcpServers": {"stripe": {"type": "http", "url": "https://mcp.stripe.com"}}}}}
    s = parse_data(data, loc("claude_code_user", "claude_code", path="/h/.claude.json"))
    by = {x.name: x for x in s}
    assert by["u"].scope == "user"
    assert by["stripe"].scope == "local" and by["stripe"].project == "/repo/app"


def test_project_scope_sets_project_root():
    s = parse_data({"mcpServers": {"a": {"command": "node", "args": ["a.js"]}}},
                   loc("mcpServers", scope="project", path="/repo/app/.cursor/mcp.json"))
    assert s[0].project == "/repo/app"


def test_user_locations_are_os_specific():
    paths = {str(l.path) for l in user_locations(Path("/h"))}
    if current_os() == "macos":
        assert "/h/Library/Application Support/Claude/claude_desktop_config.json" in paths
    elif current_os() == "linux":
        assert any(p.endswith(".config/Claude/claude_desktop_config.json") for p in paths)
    assert "/h/.cursor/mcp.json" in paths and "/h/.claude.json" in paths


def test_process_matching_and_surface():
    [gh] = parse_data({"mcpServers": {"gh": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}}},
                      loc("mcpServers"))
    cmd = ["node", "/Users/a/.npm/_npx/123/node_modules/.bin/mcp-server-github", "@modelcontextprotocol/server-github"]
    assert match_server(cmd, [gh]) is gh
    chain = [{"name": "node", "exe": "/usr/local/bin/node"},
             {"name": "Cursor Helper (Plugin)", "exe": "/Applications/Cursor.app/Contents/Frameworks/x"}]
    assert surface_from_chain(chain) == "cursor"
    assert surface_from_chain([{"name": "claude", "exe": "/usr/local/bin/claude"}]) == "claude_code"
    assert surface_from_chain([{"name": "Claude", "exe": "/Applications/Claude.app/Contents/MacOS/Claude"}]) == "claude_desktop"


def test_crontab_parsing_resolves_cd_and_redacts():
    from sensor.schedules import parse_crontab, schedule_signals
    text = ("MAILTO=x@y\n# comment\n"
            "30 7 * * 1-5 cd ~/ar-automation && /usr/bin/python3 summarize_customer_emails.py >> log.txt 2>&1\n"
            "@hourly /usr/local/bin/backup --password hunter2xyz\n"
            "0 8 * * 1 /opt/x/.venv/bin/python /opt/x/report.py --token=abc123def\n")
    jobs = parse_crontab(text, Path("/Users/praman"))
    assert [j["scripts"] for j in jobs] == [["/Users/praman/ar-automation/summarize_customer_emails.py"], ["/opt/x/report.py"]]
    assert jobs[0]["cron"] == "30 7 * * 1-5"
    assert "abc123def" not in jobs[1]["command"]
    [(raw, fp), _] = schedule_signals(jobs)
    from common.schema import fingerprint
    assert fp == fingerprint("script", "/Users/praman/ar-automation/summarize_customer_emails.py")
