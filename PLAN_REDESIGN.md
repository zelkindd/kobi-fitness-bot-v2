# Training Plan Redesign — Implementation Spec

## Goal

Replace the current strict ordered-list plan with a weekly bucket model.
Each week defines a set of workout *types* to complete. Kobi matches actual
runs to the best unmatched type for the current week, advances the week
automatically every Saturday night, and computes target pace and distance
from the user's recent fitness data — nothing is hardcoded in the plan.

---

## Current Architecture (what we're replacing)

```
training_plan:    week | day | workout_type | target_distance | target_pace | notes
plan_execution:   date | planned_workout (ID) | completed | distance_diff | pace_diff
```

Problems:
- Strict chronological order — skipping a day breaks the whole sequence
- Target pace is hardcoded at plan creation time, gets stale
- `get_next_planned_workout` returns one specific row — no flexibility
- No concept of "week is done" vs "week has passed"

---

## New Architecture

### Table: `weekly_plan`

One row per workout type per week.

```sql
CREATE TABLE weekly_plan (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    week            INTEGER NOT NULL,          -- 1-based week number in the plan
    workout_type    TEXT NOT NULL,             -- 'Easy Run' | 'Long Run' | 'Tempo' | 'Intervals'
    target_distance REAL,                      -- optional soft target in km (can be NULL)
    notes           TEXT                       -- optional coach notes for that workout
);
```

Example rows for a 3-workout week:
```
week=1, workout_type='Easy Run',  target_distance=8,  notes=''
week=1, workout_type='Long Run',  target_distance=14, notes='go slow'
week=1, workout_type='Tempo',     target_distance=6,  notes='zone 4'
```

No `day` column. No `target_pace` column — pace is computed dynamically.

---

### Table: `weekly_execution`

One row per completed workout type per calendar week.

```sql
CREATE TABLE weekly_execution (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_week       INTEGER NOT NULL,          -- which plan week (1, 2, 3...)
    calendar_week   TEXT NOT NULL,             -- ISO date of that Sunday e.g. '2026-05-10'
    workout_type    TEXT NOT NULL,             -- matched type e.g. 'Easy Run'
    run_date        TEXT NOT NULL,             -- actual run date
    actual_distance REAL,
    actual_pace     TEXT,
    distance_diff   REAL                       -- actual - target (negative = ran less)
);
```

Unique constraint: `(calendar_week, workout_type)` — one checkoff per type per week.

---

### Table: `plan_week_position`

Tracks which plan week the user is currently on and when it started.

```sql
CREATE TABLE plan_week_position (
    id              INTEGER PRIMARY KEY,       -- always 1 (single row)
    current_week    INTEGER NOT NULL,          -- current plan week number
    week_start_date TEXT NOT NULL              -- ISO date of the Sunday this week started
);
```

---

## Week Advancement Logic

**Trigger:** Every Saturday night (or on next interaction after Saturday night).

**Check in `get_current_context()`:**
1. Read `plan_week_position` → `week_start_date`
2. Compute the Saturday of that week: `week_start_date + 6 days`
3. If `today > that Saturday` → advance: `current_week += 1`, `week_start_date = last Sunday`
4. Write back to `plan_week_position`

This means advancement is lazy — it happens the next time any tool reads the context, not on a cron job. No scheduler needed.

---

## Run Matching Logic (`log_run` update)

After saving the run, attempt to match to an unmatched workout type for the current week:

```
1. Get current plan week and calendar week start from plan_week_position
2. Load weekly_plan rows for current_week
3. Load weekly_execution rows for this calendar_week
4. Find unmatched types = weekly_plan types NOT in weekly_execution for this calendar_week
5. Match actual workout_type to unmatched types:
   a. Exact match (case-insensitive) → use it
   b. No exact match → no checkoff (extra/free run)
6. If matched:
   - Compute distance_diff = actual_km - target_km
   - Insert into weekly_execution
   - Return match info in JSON result
7. If no match:
   - Return plan_status='extra' in JSON result
```

**Type matching rules:**

| Actual type logged | Matches plan slot |
|---|---|
| Easy Run | Easy Run |
| Long Run | Long Run |
| Tempo | Tempo |
| Intervals | Intervals |
| Recovery Jog | Easy Run (closest) |
| (empty / unknown) | No match — extra run |

Recovery Jog maps to Easy Run only if Easy Run slot is still open.

---

## Dynamic Pace Computation

Target pace is **not stored** in `weekly_plan`. Instead, when Kobi responds
after a run or answers "what should I run?", it calls a tool that computes
the appropriate pace from actual data.

### New tool: `get_target_pace_for_type(workout_type)`

```python
def get_target_pace_for_type(workout_type: str) -> dict:
    """
    Compute a target pace for the given workout type based on:
    - Last 5 runs of that type (actual pace average)
    - User's max HR and HR zones
    - Cap: no more than 15s/km change from last run of same type
    Returns: { pace_range: '6:10-6:25', zone: 2, rationale_he: '...' }
    """
```

Logic per type:
- **Easy Run / Long Run** → Zone 2 pace (60–70% max HR). Base = avg of last 3 easy runs. If HR in those runs was above Zone 2, add 15s/km. Cap change at 15s/km.
- **Tempo** → Zone 4 pace (80–90% max HR). Base = avg of last 3 tempo runs.
- **Intervals** → Zone 5 pace (90–100% max HR). Base = last interval session pace.
- **No history** → fall back to HR zone formula using max HR.

---

## New / Updated Tools

### `get_current_week_status()` — replaces `get_next_planned_workout`

```python
def get_current_week_status() -> dict:
    """
    Returns the current plan week, what's planned, and what's been done so far.
    Auto-advances the week if Saturday has passed.
    """
    # Returns:
    {
      "plan_week": 3,
      "calendar_week_start": "2026-05-10",
      "planned": [
        {"workout_type": "Easy Run", "target_distance": 8, "done": True,  "run_date": "2026-05-11"},
        {"workout_type": "Long Run", "target_distance": 14, "done": False, "run_date": None},
        {"workout_type": "Tempo",    "target_distance": 6,  "done": False, "run_date": None},
      ],
      "completed_count": 1,
      "total_count": 3,
      "week_ends": "2026-05-16"
    }
```

### `save_training_plan(plan_text)` — rewrite parser

Same interface (paste free text), but now parses into `weekly_plan` rows instead of `training_plan` rows.

Parser prompt to DeepSeek:
```
Extract a weekly training plan. For each week return:
  week (int), workouts (list of {workout_type, target_distance, notes})
workout_type must be one of: Easy Run, Long Run, Tempo, Intervals
Return JSON only.
```

Clears `weekly_plan` and `weekly_execution` before inserting.
Resets `plan_week_position` to week=1, week_start=last Sunday.

### `get_current_week_status()` — called in `get_current_context()`

The context block injected into every system prompt gains a new field:

```
מצב נוכחי:
...
שבוע תכנית: 3 | נשאר: ריצה ארוכה, טמפו
```

### `log_run()` — updated return

Already returns JSON (as of v2.4). Add new fields:
- `plan_status`: `'matched' | 'extra' | 'none'`  (`'extra'` = ran but no slot matched)
- `matched_type`: which slot was checked off
- `remaining_this_week`: list of workout types still open

---

## System Prompt Changes

Replace all references to `get_next_planned_workout` with `get_current_week_status`.

New instruction block:
```
כשמשתמש שואל מה נשאר השבוע: קרא get_current_week_status והצג מה בוצע ומה נשאר.
כשמשתמש שואל מה לרוץ היום: קרא get_current_week_status לראות מה פתוח, ואז get_target_pace_for_type לקצב.
אחרי log_run: אם plan_status='extra' — אמור "ריצה חופשית, לא מסמנת כלום בתכנית".
```

---

## Migration Steps (in order)

1. **Schema** — add `weekly_plan`, `weekly_execution`, `plan_week_position` to `init_db()`. Keep old tables for now (don't drop until stable).

2. **`save_training_plan`** — rewrite to parse into `weekly_plan`. Update parser prompt.

3. **`get_current_week_status`** — new tool. Includes lazy week-advance logic.

4. **`log_run`** — replace plan-match block with new bucket matching logic.

5. **`get_target_pace_for_type`** — new tool.

6. **`get_current_context`** — add week status to the injected context block.

7. **System prompt** — update all plan-related instructions.

8. **`recommend_next_workout`** — update to call `get_current_week_status` + `get_target_pace_for_type` instead of `get_next_planned_workout`.

9. **Drop old tables** — once stable: drop `training_plan`, `plan_execution`. Remove old tools.

10. **`/setweek` command** — update to write to `plan_week_position` instead of inserting a synthetic `plan_execution` row.

---

## What Does NOT Change

- `log_km_splits` / `get_km_splits` / `get_splits_history` — untouched
- `get_recent_runs`, `get_runs_by_type` — untouched
- `get_hr_zones`, `get_hr_pace_analysis` — untouched
- `log_weight`, `log_nutrition` — untouched
- Telegram bot structure, onboarding, all commands except `/setweek` — untouched
- Free-text plan input via `/setplan` — same UX, new parser

---

## Open Questions (decide before implementing)

1. **Missed workout carry-over**: if Long Run isn't done by Saturday, does it carry to next week as an extra slot, or just disappear? Recommendation: disappear — the new week's plan takes over. Coach can address it verbally.

2. **Week 0 / no plan**: if no plan is loaded, `get_current_week_status` returns empty. `recommend_next_workout` falls back to the existing "no plan" case (easy run at average distance).

3. **Plan re-upload**: uploading a new plan mid-training — should it reset to week 1 or try to continue from current week? Recommendation: always reset to week 1 and tell the user.
