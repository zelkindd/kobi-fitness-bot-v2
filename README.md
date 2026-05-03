# Kobi Fitness Bot v2

Personal Telegram fitness coach powered by DeepSeek AI and MCP architecture.

## What it does

- Pulls your latest run from Strava and gives feedback
- Compares runs against your training plan
- Tracks weight, nutrition, and progress
- Answers natural Hebrew questions about your fitness data
- Fully autonomous — no keywords needed, just talk to it

## Architecture

```
Telegram
   ↓
bot.py  (host + agent loop)
   ↓
DeepSeek API  ←→  MCP Tools
                  ├── mcp_sqlite_server.py  (16 DB tools)
                  └── mcp_strava_server.py  (Strava fetch)
```

Instead of hardcoded commands, DeepSeek decides which tools to call based on what you say. All tools are discovered once at startup.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Telegram bot + DeepSeek agent loop |
| `mcp_sqlite_server.py` | FastMCP server exposing SQLite tools |
| `mcp_strava_server.py` | FastMCP server exposing Strava tools |
| `kobi.db` | SQLite database (not committed) |
| `.env` | Secrets (not committed) |

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env  # fill in your keys
venv/bin/python bot.py
```

## .env variables

```
TELEGRAM_TOKEN=
DEEPSEEK_API_KEY=
STRAVA_CLIENT_ID=
STRAVA_CLIENT_SECRET=
STRAVA_REFRESH_TOKEN=
```

## Systemd

```bash
sudo systemctl start kobi
sudo systemctl status kobi
sudo journalctl -u kobi -f
```

## Telegram commands

| Command | Description |
|---|---|
| `/setplan` | Paste a training plan |
| `/setweek 2 1` | Set current position in plan |
| `/settarget 75` | Set weight target |
| `/setup` | Redo onboarding |
| `/balance` | Check remaining DeepSeek API credit |

## Tools exposed to DeepSeek

**SQLite server:** `get_profile`, `update_profile`, `log_weight`, `get_recent_weights`, `log_run`, `get_recent_runs`, `get_runs_by_type`, `log_nutrition`, `get_recent_nutrition`, `get_training_plan`, `get_next_planned_workout`, `save_training_plan`, `set_plan_position`, `log_plan_execution`, `get_weekly_stats`, `update_workout_paces`, `log_km_splits`, `get_km_splits`, `get_splits_history`, `get_hr_zones`

**Strava server:** `get_latest_activity`, `get_recent_activities_with_splits`, `estimate_max_hr_from_strava`
