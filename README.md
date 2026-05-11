# Kobi Fitness Bot v2.3

Personal Telegram fitness coach powered by DeepSeek AI and MCP architecture. Coaches entirely in Hebrew. No keywords needed — just talk to it.

---

## What it does

- **Auto-syncs runs from Strava** — Strava webhook fires when you finish a run; Kobi logs it and sends you a Telegram notification automatically
- Pulls your latest run from Strava on demand and stores it with per-km split data
- Compares every logged run against your loaded training plan and auto-advances your position in the plan when a workout matches
- Recommends the next workout with full Python-computed logic: distance, pace range, workout type, and a one-line Hebrew rationale
- Injects live context (today's date in Hebrew, days since last run, last run summary, plan position) into every LLM call — so the model never has to compute dates itself
- Tracks weight entries over time and answers trend questions
- Analyses meals from photos and logs nutrition
- Calculates heart-rate training zones from stored max HR or age
- Answers natural Hebrew questions about your fitness data autonomously

---

## Architecture

```
Telegram
   ↓
bot.py  (Telegram host + DeepSeek agent loop + aiohttp webhook server)
   │
   ├── calls get_current_context() once per message
   │   → injects live "מצב נוכחי" block into system prompt
   │
   ├── DeepSeek API  ←→  MCP Tools (discovered at startup, cached)
   │                     ├── mcp_sqlite_server.py  (23 tools)
   │                     └── mcp_strava_server.py  (3 Strava tools)
   │
   └── aiohttp webhook server on port 8080
       → nginx proxies GET/POST /strava/webhook → 127.0.0.1:8080
       → Strava pushes activity events here
       → auto log_run + log_km_splits + Telegram notification
```

DeepSeek receives a system prompt with hard-coded current facts (date, days since last run, plan position) plus a full tool list. It decides which tools to call. All coaching *reasoning* — workout recommendations, plan matching, pace calculations — lives in deterministic Python tools, not in the prompt. The model's job is orchestration and narration.

Tool schemas are discovered from both MCP stdio servers once at startup via `_load_tools()` and cached in `_tools_cache` / `_tool_server_map`. Each tool call opens a fresh subprocess session to the appropriate server.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Telegram bot, DeepSeek agent loop, system prompt builder, aiohttp Strava webhook server |
| `mcp_sqlite_server.py` | FastMCP server — 22 SQLite-backed tools |
| `mcp_strava_server.py` | FastMCP server — 3 Strava API tools |
| `kobi.db` | SQLite database (not committed) |
| `.env` | Secrets (not committed) |

---

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

---

## Telegram commands

| Command | Description |
|---|---|
| `/setplan` | Paste a free-text training plan — DeepSeek parses and stores it |
| `/setweek 2 1` | Manually set current position in plan (week, run number) |
| `/settarget 75` | Set weight target in kg |
| `/setup` | Redo onboarding (name, coaching style, nutrition rules, weight, age) |
| `/balance` | Check remaining DeepSeek API credit |

Keyboard buttons: `📊 סיכום`, `🗓 תכנית`, `📈 התקדמות`, `❓ עזרה`

---

## Tools exposed to DeepSeek

### SQLite server (`mcp_sqlite_server.py`)

#### Profile

| Tool | Description |
|---|---|
| `get_profile` | Returns name, coaching style, nutrition rules, weight goal, training days, weight target, age, max HR. Uses explicit column names — safe against schema drift. |
| `update_profile` | Updates a single profile field. Allowed: `user_name`, `personality`, `nutrition_rules`, `weight_goal`, `training_days`, `weight_target`, `age`, `max_heart_rate`. |
| `get_current_context` | **New in v2.1.** Returns today's ISO date, today's date in Hebrew (e.g. "יום שני, 11 במאי 2026"), Hebrew weekday name, days since last run, last run summary (date/distance/type), and current plan position (week/run_num). Called once per message in `run_agent` before the LLM loop; result is injected into the system prompt as a "מצב נוכחי" block. |

#### Weight

| Tool | Description |
|---|---|
| `log_weight` | Saves a weight entry. Defaults to today if no date given. |
| `get_recent_weights` | Returns last N weight entries (date, kg). Default limit 10. |

#### Runs

| Tool | Description |
|---|---|
| `log_run` | Saves a completed run. **v2.1: auto-advances plan position** — after saving, checks if this run matches the next planned workout (type + distance within ±20%); if so, inserts a `plan_execution` row and includes it in the return message. |
| `get_recent_runs` | Returns last N runs with date, distance, pace, HR, feedback, type. |
| `get_runs_by_type` | Returns recent runs filtered by type (Easy Run / Long Run / Tempo / Intervals). |
| `log_km_splits` | Saves per-km split data for a run date. Replaces any existing splits for that date. |
| `get_km_splits` | Returns per-km splits for a specific run date. |
| `get_splits_history` | Returns km splits for the last N runs that have split data. |

#### Workout recommendations

| Tool | Description |
|---|---|
| `recommend_next_workout` | **New in v2.1.** Full Python decision tree returning a structured recommendation with `distance_km`, `pace_range`, `workout_type`, `intensity_note`, and `rationale_he`. Call this whenever the user asks what to run. See decision logic below. |
| `get_hr_pace_analysis` | **New in v2.2.** Groups last 20 runs by workout type, splits into early/late halves, and compares HR and pace trends. Returns a per-type classification (מצוין / טוב / יציב / לבדוק) with a Hebrew note and an overall `summary_he`. Call this when the user asks about fitness progress or whether they're improving. |

#### Nutrition

| Tool | Description |
|---|---|
| `log_nutrition` | Saves a meal entry (description, type, calories, feedback). |
| `get_recent_nutrition` | Returns last N nutrition entries. |

#### Training plan

| Tool | Description |
|---|---|
| `get_training_plan` | Returns the full loaded plan (all weeks and runs). |
| `get_next_planned_workout` | Returns the next unexecuted workout in the plan based on `plan_execution` history. Wraps around to the start if the plan is fully completed. |
| `save_training_plan` | Parses a free-text training plan via DeepSeek and stores it. Replaces the current plan. |
| `set_plan_position` | Manually sets which workout is next (inserts a synthetic `plan_execution` row). |
| `log_plan_execution` | Records whether a specific planned workout was completed, with distance and pace diffs. |
| `update_workout_paces` | Updates target pace for all future workouts of a given type. Used for progressive overload when the user is consistently beating targets. |
| `get_weekly_stats` | Returns this week's runs, weight entries, nutrition logs, and last completed plan ID. |

#### Heart rate

| Tool | Description |
|---|---|
| `get_hr_zones` | Calculates zones 1–5 from stored `max_heart_rate`; falls back to `220 - age` if max HR is not set. Returns bpm ranges and training purpose per zone. |

---

### Strava server (`mcp_strava_server.py`)

| Tool | Description |
|---|---|
| `get_latest_activity` | Fetches the most recent run from Strava with distance, duration, pace, avg HR, and per-km splits. |
| `get_recent_activities_with_splits` | Fetches last N runs from Strava, each with per-km splits. |
| `estimate_max_hr_from_strava` | Scans last 100 Strava activities for the highest recorded heart rate. |

---

## `recommend_next_workout` — decision logic

All reasoning is in Python. The model calls this tool and narrates the result; it never computes the recommendation itself.

```
days_since_last_run ≥ 4?
  YES → Easy Run, 80% of planned distance, pace = slow end of planned range + 15s
        rationale: "X ימים בלי ריצה — חזרה בקלילות"

days_since ≤ 1 AND last workout was Long Run / Tempo / Intervals?
  YES → Recovery Jog, 5km, easy pace
        rationale: "רצת [type] לאחרונה — מנוחה או 5 ק״מ התאוששות"

Training plan loaded with a next workout?
  YES → Follow the plan exactly (distance, pace range, type, notes)
        rationale: "לפי תכנית: שבוע X ריצה Y"

No plan loaded?
  → Easy Run, average distance of last 3 runs, average pace + 15s
    rationale: "אין תכנית טעונה — ריצה קלה X ק״מ"
```

Pace helpers (`_pace_str_to_secs`, `_secs_to_pace_str`, `_make_easy_pace_range`) are private module functions. They handle ranges like `"6:15-6:30"` and `/km` suffixes.

---

## `log_run` — auto-advance logic

Constant: `PLAN_MATCH_DISTANCE_TOLERANCE = 0.20` (±20%)

After every `log_run`:
1. Calls `get_next_planned_workout()` to find the pending workout.
2. Checks **type match**: case-insensitive equality; if either side is empty the type check passes (allows untyped runs to match).
3. Checks **distance match**: `|actual - planned| / planned ≤ 0.20`.
4. If both pass: inserts a `plan_execution` row with `completed=1` and the distance delta.
5. Return message includes e.g. `"ריצה נשמרה. סימנתי את שבוע 2 ריצה 3 כבוצע."` — the model reads this and can tell the user the plan advanced.

The entire block is wrapped in `try/except` so a plan-matching failure never prevents the run from being saved.

---

## Context injection — how it works

Every call to `run_agent` starts with:

```python
ctx_raw = await call_tool("get_current_context", {})
ctx = json.loads(ctx_raw)
system_prompt = build_system_prompt(context=ctx)
```

`build_system_prompt(context)` inserts a `מצב נוכחי:` block near the top of the system prompt:

```
מצב נוכחי:
היום: יום שני, 11 במאי 2026
ימים מהריצה האחרונה: 1
ריצה אחרונה: 2026-05-10, 10.0 ק״מ, Easy Run
מיקום בתכנית: שבוע 2, ריצה 3
```

This means DeepSeek always knows the current date and run history before it reads any user message. It no longer needs to call `get_recent_runs` just to know when the last run was.

---

## Known schema note

The production `kobi.db` has a legacy `workout_type` column in `user_profile` (added by an old migration) that sits between `weight_target` and `age`. `get_profile()` was updated in v2.1 to use explicit `SELECT` column names instead of positional `SELECT *`, so it reads the correct values regardless of extra columns.

---

## Strava Webhook

Kobi runs an aiohttp server on `127.0.0.1:8080` alongside the Telegram polling loop. Nginx routes `GET/POST /strava/webhook` to it.

**Verification (GET):** Strava sends `hub.mode=subscribe`, `hub.verify_token`, `hub.challenge` — Kobi returns `{"hub.challenge": "..."}` if the token matches `STRAVA_VERIFY_TOKEN` in `.env`.

**Events (POST):** When Strava fires an `activity create` event, Kobi:
1. Fetches the full activity from Strava API by `object_id`
2. Calls `log_run` to save distance, pace, HR, duration
3. Calls `log_km_splits` to save per-km split data
4. Sends a Telegram message: "זיהיתי ריצה חדשה בסטרבה — X ק״מ ב-Y/ק״מ דופק Z. שמרתי אותה."
5. If the run matched the next planned workout and auto-advanced the plan, adds that to the notification

**Register the webhook with Strava** (one-time, run after any server IP change):
```bash
curl -X POST https://www.strava.com/api/v3/push_subscriptions \
  -F client_id=232444 \
  -F client_secret=49edc5db9f87747a5a44441e257336ef3489f7df \
  -F callback_url=http://151.145.95.15/strava/webhook \
  -F verify_token=kobi-strava-verify
```

---

## v2.3 changelog

| # | Change | File | Why |
|---|---|---|---|
| 1 | Strava webhook server (aiohttp on port 8080) | `bot.py` | Auto-log runs from Strava without user input |
| 2 | nginx `/strava/webhook` → `127.0.0.1:8080` | `/etc/nginx/sites-enabled/kobi-strava-webhook.conf` | Expose webhook on public port 80 |
| 3 | `STRAVA_VERIFY_TOKEN` added to `.env` | `.env` | Webhook verification secret |
| 4 | `aiohttp` added to dependencies | `requirements.txt` | Async HTTP server for webhook |

## v2.2 changelog

| # | Change | File | Why |
|---|---|---|---|
| 1 | `get_weekly_stats` uses Israeli Sunday–Saturday week boundary | `mcp_sqlite_server.py` | Was using rolling 7-day window, not a real week |
| 2 | New tool `get_hr_pace_analysis` | `mcp_sqlite_server.py` | Analyzes HR vs pace trend across runs — detects fitness gain, plateau, or overtraining |
| 3 | Fixed 📈 התקדמות button to use a fixed 4-section template | `bot.py` | Response was free-form and inconsistent each time |
| 4 | Added `CLAUDE.md` | project root | Instructs Claude to auto-update README and commit/push after every code change |

## v2.1 changelog

| # | Change | File | Why |
|---|---|---|---|
| 1 | Added `age` and `max_heart_rate` to `update_profile` whitelist | `mcp_sqlite_server.py` | Onboarding silently dropped age; HR zone tool couldn't be seeded via model |
| 1b | Fixed `get_profile()` to use named-column SELECT | `mcp_sqlite_server.py` | Legacy extra column was offsetting positional index reads for age/max_hr |
| 2 | New tool `get_current_context` | `mcp_sqlite_server.py` | Gives model deterministic date/recency facts without LLM computation |
| 2 | `run_agent` fetches context, injects into system prompt | `bot.py` | Eliminates "what day is it" and "days since last run" reasoning errors |
| 3 | New tool `recommend_next_workout` | `mcp_sqlite_server.py` | Single tool call answers "what should I run?" with Python decision logic |
| 4 | `log_run` auto-advances `plan_execution` on match | `mcp_sqlite_server.py` | `plan_execution` was always empty; plan position never advanced automatically |
