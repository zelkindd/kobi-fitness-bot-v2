# Kobi Fitness Bot v2.5

Personal Telegram fitness coach powered by DeepSeek AI and MCP architecture. Coaches entirely in Hebrew. No keywords needed — just talk to it.

---

## What it does

- Pulls your latest run from Strava and stores it with per-km split data
- Matches every logged run to the correct weekly bucket in the training plan (no more strict ordering — any workout type in the week's bucket can be checked off in any order)
- Recommends the next workout with full Python-computed logic: dynamic pace from real run history + HR zones, no hardcoded targets
- Injects live context (today's date in Israeli timezone, days since last run, last run summary, current week status) into every LLM call
- Auto-advances the plan week every Saturday night (lazy — happens on next interaction)
- Tracks weight entries over time and answers trend questions
- Analyses meals from photos and logs nutrition
- Calculates heart-rate training zones from stored max HR or age
- Answers natural Hebrew questions about your fitness data autonomously

---

## Architecture

```
Telegram
   ↓
bot.py  (Telegram host + DeepSeek agent loop)
   │
   ├── calls get_current_context() once per message
   │   → injects live "מצב נוכחי" block into system prompt
   │
   └── DeepSeek API  ←→  MCP Tools (discovered at startup, cached)
                         ├── mcp_sqlite_server.py  (23 tools)
                         └── mcp_strava_server.py  (3 Strava tools)
```

DeepSeek receives a system prompt with hard-coded current facts (today in Israeli timezone, days since last run, plan week status) plus a full tool list. It decides which tools to call. All coaching *reasoning* — workout recommendations, plan matching, pace calculations — lives in deterministic Python tools, not in the prompt. The model's job is orchestration and narration.

Tool schemas are discovered from both MCP stdio servers once at startup via `_load_tools()` and cached in `_tools_cache` / `_tool_server_map`. Each tool call opens a fresh subprocess session to the appropriate server.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Telegram bot, DeepSeek agent loop, system prompt builder |
| `mcp_sqlite_server.py` | FastMCP server — 25 SQLite-backed tools |
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
| `get_current_context` | Returns today's ISO date in **Israel timezone**, Hebrew date string, days since last run, last run summary, and current week plan status. Called once per message before the LLM loop; injected into the system prompt as a "מצב נוכחי" block. |

#### Weight

| Tool | Description |
|---|---|
| `log_weight` | Saves a weight entry. Defaults to today if no date given. |
| `get_recent_weights` | Returns last N weight entries (date, kg). Default limit 10. |

#### Runs

| Tool | Description |
|---|---|
| `log_run` | Saves a completed run. **v2.5: weekly bucket matching** — after saving, looks for an unmatched slot in the current week's `weekly_plan` that matches the workout type; checks it off in `weekly_execution`. Returns `plan_status` (`matched`/`extra`/`none`), `matched_type`, and `remaining_this_week`. |
| `get_recent_runs` | Returns last N runs with date, distance, pace, HR, feedback, type. |
| `get_runs_by_type` | Returns recent runs filtered by type (Easy Run / Long Run / Tempo / Intervals). |
| `log_km_splits` | Saves per-km split data for a run date. Replaces any existing splits for that date. |
| `get_km_splits` | Returns per-km splits for a specific run date. |
| `get_splits_history` | Returns km splits for the last N runs that have split data. |

#### Workout recommendations

| Tool | Description |
|---|---|
| `recommend_next_workout` | Full Python decision tree returning a structured recommendation with `distance_km`, `pace_range`, `workout_type`, `intensity_note`, and `rationale_he`. Uses `get_current_week_status` + `get_target_pace_for_type` internally. |
| `get_hr_pace_analysis` | Groups last 20 runs by workout type, splits into early/late halves, and compares HR and pace trends. Returns a per-type classification (מצוין / טוב / יציב / לבדוק) with a Hebrew note and an overall `summary_he`. |

#### Nutrition

| Tool | Description |
|---|---|
| `log_nutrition` | Saves a meal entry (description, type, calories, feedback). |
| `get_recent_nutrition` | Returns last N nutrition entries. |

#### Training plan (new weekly bucket model)

| Tool | Description |
|---|---|
| `get_current_week_status` | Returns current plan week, what's planned, what's been done this calendar week, and what's remaining. Auto-advances the week if Saturday has passed (lazy). Primary plan tool — replaces `get_next_planned_workout` for new plans. |
| `get_target_pace_for_type` | Computes target pace for a workout type from the last 5 runs of that type + HR zones. Caps change at 15s/km. Returns `{pace_range, zone, rationale_he}`. |
| `save_training_plan` | Parses a free-text plan via DeepSeek into `weekly_plan` rows (type + distance per week, no day/pace). Resets to week 1. |
| `set_plan_position` | Sets the current plan week. Writes to `plan_week_position` for new plans, or falls back to old `plan_execution` logic for legacy plans. |
| `get_weekly_stats` | Returns this week's runs, weight entries, and nutrition logs (Israeli Sunday–Saturday week). |

#### Legacy plan tools (kept for backward compat)

| Tool | Description |
|---|---|
| `get_training_plan` | Returns the full legacy `training_plan` table. |
| `get_next_planned_workout` | Returns next workout from legacy `training_plan`/`plan_execution`. Use `get_current_week_status` for new plans. |
| `log_plan_execution` | Records completion of a legacy planned workout. |
| `update_workout_paces` | Updates target pace for all workouts of a type in the legacy plan. |

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
  YES → Easy Run, 80% of next planned distance, pace from get_target_pace_for_type
        rationale: "X ימים בלי ריצה — חזרה בקלילות"

days_since ≤ 1 AND last workout was Long Run / Tempo / Intervals?
  YES → Recovery Jog, 5km, easy pace
        rationale: "רצת [type] לאחרונה — מנוחה או 5 ק״מ התאוששות"

weekly_plan has remaining workouts this week?
  YES → first remaining type, target_distance from plan, pace from get_target_pace_for_type
        rationale: "לפי תכנית שבוע X: [type] Y ק״מ"

No plan or all done this week?
  → Easy Run, average distance of last 3 runs, pace from get_target_pace_for_type
    rationale: "אין אימון פתוח בתכנית — ריצה קלה X ק״מ"
```

---

## `log_run` — weekly bucket matching (v2.5)

After every `log_run`:
1. Calls `_maybe_advance_week()` to auto-advance the plan week if Saturday has passed.
2. Loads `weekly_plan` rows for the current plan week.
3. Loads `weekly_execution` rows for the current calendar week (Sunday ISO date as key).
4. Finds unmatched slots = planned types not yet in `weekly_execution` this calendar week.
5. Matches `workout_type` against unmatched slots (case-insensitive; `Recovery Jog` aliases to `Easy Run`).
6. If matched: inserts into `weekly_execution` with distance diff; returns `plan_status="matched"`.
7. If no match but type given: returns `plan_status="extra"` (free run, no slot consumed).
8. Returns `remaining_this_week`: list of still-open workout types.

The entire block is wrapped in `try/except` so a match failure never prevents the run from being saved.

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
היום: יום רביעי, 13 במאי 2026
ימים מהריצה האחרונה: 2
ריצה אחרונה: 2026-05-11, 10.0 ק״מ, Easy Run
שבוע תכנית: 3 | נשאר: Long Run, Tempo
```

The date is read in **Israel timezone** (`Asia/Jerusalem` via `zoneinfo`) so it stays correct after 9pm when the UTC server date flips to the next day while Israel is still in the same day.

---

## Known schema note

The production `kobi.db` has a legacy `workout_type` column in `user_profile` (added by an old migration) that sits between `weight_target` and `age`. `get_profile()` was updated in v2.1 to use explicit `SELECT` column names instead of positional `SELECT *`, so it reads the correct values regardless of extra columns.

---

## v2.5 changelog

| # | Change | File | Why |
|---|---|---|---|
| 1 | All `date.today()` → `today_israel()` using `zoneinfo.ZoneInfo("Asia/Jerusalem")` | `mcp_sqlite_server.py` | Server runs UTC; after 9pm Israel time the UTC date was one day behind, causing Kobi to report the wrong day |
| 2 | New table `weekly_plan` — one row per workout type per week (no day, no target_pace) | `mcp_sqlite_server.py` | Replaces strict ordered plan with a flexible weekly bucket model |
| 3 | New table `weekly_execution` — one row per completed workout type per calendar week | `mcp_sqlite_server.py` | Tracks which slots are done this week; unique on `(calendar_week, workout_type)` |
| 4 | New table `plan_week_position` — single-row tracker for current plan week + start date | `mcp_sqlite_server.py` | Enables lazy week advancement each Saturday |
| 5 | New tool `get_current_week_status` | `mcp_sqlite_server.py` | Returns planned/done/remaining for this week; auto-advances week if Saturday passed |
| 6 | New tool `get_target_pace_for_type` | `mcp_sqlite_server.py` | Computes pace dynamically from last 5 runs of that type + HR zones; caps change at 15s/km |
| 7 | `save_training_plan` rewritten | `mcp_sqlite_server.py` | Parses into `weekly_plan` rows; resets to week 1; no more hardcoded target_pace |
| 8 | `log_run` — replaced plan-match logic with weekly bucket matching | `mcp_sqlite_server.py` | Checks off the matching open slot in `weekly_execution`; returns `remaining_this_week` |
| 9 | `set_plan_position` — writes to `plan_week_position` for new plans | `mcp_sqlite_server.py` | Old logic inserted synthetic `plan_execution` rows |
| 10 | `recommend_next_workout` — uses `get_current_week_status` + `get_target_pace_for_type` | `mcp_sqlite_server.py` | Dynamic pace instead of stale hardcoded values |
| 11 | `get_current_context` — injects week status instead of old plan position | `mcp_sqlite_server.py` | System prompt now shows "שבוע 3 | נשאר: Long Run, Tempo" |
| 12 | System prompt: `get_plan_position` → `get_current_week_status`; `get_next_planned_workout` → `get_current_week_status`; new plan query instructions | `bot.py` | `get_plan_position` was a nonexistent tool; progress format updated for new model |

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
