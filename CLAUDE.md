# Kobi v2 — Claude Instructions

## After every code change

1. **Update README.md** to reflect what changed — tools table, decision logic, changelog table, architecture notes, whatever is affected. Keep it accurate and complete.
2. **Commit and push** all changed files (including README.md) in a single commit with a clear message describing what was done and why.

Do this without being asked. Every session that modifies code must end with an up-to-date README and a pushed commit.

## Stack

- `bot.py` — Telegram bot, DeepSeek agent loop, system prompt builder
- `mcp_sqlite_server.py` — FastMCP server, all SQLite-backed tools
- `mcp_strava_server.py` — FastMCP server, Strava API tools
- Service runs as `kobi` systemd unit — restart with `sudo systemctl restart kobi` after any change to `bot.py` or `mcp_sqlite_server.py`

## Week boundary

Israeli week: Sunday–Saturday. Use `(today.weekday() + 1) % 7` to get days since Sunday.

## Key conventions

- All coaching logic (recommendations, pace math, plan matching) lives in deterministic Python tools — never left to the LLM to compute
- System prompt is rebuilt on every message via `build_system_prompt(context)` with live context injected from `get_current_context()`
- Tool schemas are cached at startup in `_tools_cache` / `_tool_server_map`
