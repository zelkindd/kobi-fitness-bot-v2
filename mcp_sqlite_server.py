import sqlite3
import os
import json
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from fastmcp import FastMCP
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "kobi.db")
mcp = FastMCP("kobi-sqlite")

PLAN_MATCH_DISTANCE_TOLERANCE = 0.20
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

HEBREW_WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
HEBREW_MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
                 "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]


def today_israel() -> date:
    """Return today's date in Israel timezone (server runs UTC)."""
    return datetime.now(ISRAEL_TZ).date()


def get_connection():
    return sqlite3.connect(DB_PATH)


def _pace_str_to_secs(pace_str: str) -> int:
    """Convert '6:30' or '6:30/km' to seconds (390). Returns 0 on failure."""
    if not pace_str:
        return 0
    try:
        p = str(pace_str).strip().replace("/km", "").strip()
        if "-" in p:
            parts = p.split("-")
            vals = [_pace_str_to_secs(x.strip()) for x in parts]
            vals = [v for v in vals if v > 0]
            return sum(vals) // len(vals) if vals else 0
        m, s = p.split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return 0


def _secs_to_pace_str(secs: int) -> str:
    """Convert 390 to '6:30'."""
    if secs <= 0:
        return "לפי תחושה"
    return f"{secs // 60}:{secs % 60:02d}"


def _make_easy_pace_range(planned_pace: str, runs: list) -> str:
    """Return a relaxed pace range: slow end of planned range +15s, or avg run pace +15s."""
    if planned_pace:
        p = planned_pace.strip().replace("/km", "")
        if "-" in p:
            vals = [_pace_str_to_secs(x.strip()) for x in p.split("-")]
            vals = [v for v in vals if v > 0]
            slow = max(vals) if vals else 0
        else:
            slow = _pace_str_to_secs(p)
        if slow > 0:
            return f"{_secs_to_pace_str(slow)}-{_secs_to_pace_str(slow + 15)}"
    secs_list = [_pace_str_to_secs(r.get("pace", "")) for r in (runs or [])]
    secs_list = [s for s in secs_list if s > 0]
    if secs_list:
        avg = sum(secs_list) // len(secs_list)
        return f"{_secs_to_pace_str(avg)}-{_secs_to_pace_str(avg + 15)}"
    return "לפי תחושה"


def _current_week_start(ref: date = None) -> date:
    """Return the most recent Sunday (Israeli week starts Sunday)."""
    d = ref or today_israel()
    days_since_sunday = (d.weekday() + 1) % 7
    return d - timedelta(days=days_since_sunday)


def _maybe_advance_week(conn) -> int | None:
    """
    Lazily advance plan_week_position if the current week's Saturday has passed.
    Returns current_week (possibly advanced), or None if no position row exists.
    """
    c = conn.cursor()
    c.execute("SELECT current_week, week_start_date FROM plan_week_position WHERE id=1")
    row = c.fetchone()
    if not row:
        return None
    current_week, week_start_str = row
    try:
        week_start = date.fromisoformat(week_start_str)
    except Exception:
        return current_week

    week_saturday = week_start + timedelta(days=6)
    today = today_israel()

    if today > week_saturday:
        new_week_start = _current_week_start(today)
        weeks_passed = max(1, (new_week_start - week_start).days // 7)
        new_week = current_week + weeks_passed
        c.execute(
            "UPDATE plan_week_position SET current_week=?, week_start_date=? WHERE id=1",
            (new_week, new_week_start.isoformat())
        )
        conn.commit()
        return new_week
    return current_week


def init_db():
    conn = get_connection()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS user_profile (
        id INTEGER PRIMARY KEY,
        bot_name TEXT DEFAULT 'Kobi',
        user_name TEXT,
        personality TEXT,
        nutrition_rules TEXT,
        weight_goal TEXT,
        training_days INTEGER,
        weight_target REAL,
        age INTEGER,
        max_heart_rate INTEGER
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        distance REAL,
        duration TEXT,
        pace TEXT,
        avg_heart_rate INTEGER,
        bot_feedback TEXT,
        workout_type TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS weight (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        weight REAL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS nutrition (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        meal_type TEXT,
        description TEXT,
        calories INTEGER,
        feedback TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS training_plan (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        week INTEGER,
        day TEXT,
        workout_type TEXT,
        target_distance REAL,
        target_pace TEXT,
        notes TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS plan_execution (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        planned_workout TEXT,
        completed INTEGER DEFAULT 0,
        distance_diff REAL,
        pace_diff TEXT,
        skip_reason TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS km_splits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT,
        km INTEGER,
        pace TEXT,
        pace_seconds INTEGER,
        distance_km REAL
    )''')

    # Weekly bucket plan tables
    c.execute('''CREATE TABLE IF NOT EXISTS weekly_plan (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        week            INTEGER NOT NULL,
        workout_type    TEXT NOT NULL,
        target_distance REAL,
        notes           TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS weekly_execution (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_week       INTEGER NOT NULL,
        calendar_week   TEXT NOT NULL,
        workout_type    TEXT NOT NULL,
        run_date        TEXT NOT NULL,
        actual_distance REAL,
        actual_pace     TEXT,
        distance_diff   REAL
    )''')

    # Migrate: old index was (calendar_week, workout_type) which blocked 2 Easy Runs in a week.
    # New index: (calendar_week, run_date) — one entry per day per week is the right dedup key.
    c.execute("DROP INDEX IF EXISTS ux_weekly_exec")
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS ux_weekly_exec
        ON weekly_execution(calendar_week, run_date)''')

    c.execute('''CREATE TABLE IF NOT EXISTS plan_week_position (
        id              INTEGER PRIMARY KEY,
        current_week    INTEGER NOT NULL,
        week_start_date TEXT NOT NULL
    )''')

    for col, coltype in [("weight_target", "REAL"), ("age", "INTEGER"), ("max_heart_rate", "INTEGER")]:
        try:
            c.execute(f"ALTER TABLE user_profile ADD COLUMN {col} {coltype}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    for col, coltype in [("workout_type", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE runs ADD COLUMN {col} {coltype}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    c.execute("SELECT COUNT(*) FROM user_profile")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO user_profile (bot_name) VALUES ('Kobi')")

    conn.commit()
    conn.close()


# ── Profile ──────────────────────────────────────────────────────────────────

@mcp.tool()
def get_profile() -> dict:
    """Get the user's profile: name, coaching style, nutrition rules, weight goal, training days, weight target."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT bot_name, user_name, personality, nutrition_rules, weight_goal, "
        "training_days, weight_target, age, max_heart_rate FROM user_profile WHERE id=1"
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return {}
    return {
        "bot_name": row[0],
        "user_name": row[1],
        "personality": row[2],
        "nutrition_rules": row[3],
        "weight_goal": row[4],
        "training_days": row[5],
        "weight_target": row[6],
        "age": row[7],
        "max_heart_rate": row[8],
    }


@mcp.tool()
def update_profile(field: str, value: str) -> str:
    """Update a single field in the user profile. Fields: user_name, personality, nutrition_rules, weight_goal, training_days, weight_target."""
    allowed = {"user_name", "personality", "nutrition_rules", "weight_goal", "training_days", "weight_target", "age", "max_heart_rate"}
    if field not in allowed:
        return f"שדה לא חוקי: {field}"
    conn = get_connection()
    c = conn.cursor()
    c.execute(f"UPDATE user_profile SET {field}=? WHERE id=1", (value,))
    conn.commit()
    conn.close()
    return "עודכן בהצלחה"


@mcp.tool()
def get_current_context() -> dict:
    """Return today's date (Israel timezone), days since last run, last run summary, and current week plan status."""
    today = today_israel()
    weekday_he = HEBREW_WEEKDAYS[today.weekday()]
    month_he = HEBREW_MONTHS[today.month - 1]
    today_hebrew = f"יום {weekday_he}, {today.day} ב{month_he} {today.year}"

    conn = get_connection()
    c = conn.cursor()

    c.execute("SELECT date, distance, workout_type FROM runs ORDER BY date DESC LIMIT 1")
    row = c.fetchone()
    last_run = None
    days_since = None
    if row:
        last_run = {"date": row[0], "distance_km": row[1], "workout_type": row[2]}
        try:
            last_date = date.fromisoformat(row[0])
            days_since = (today - last_date).days
        except Exception:
            pass

    conn.close()

    ctx = {
        "today_iso": today.isoformat(),
        "today_hebrew": today_hebrew,
        "weekday": weekday_he,
        "days_since_last_run": days_since,
        "last_run": last_run,
    }

    try:
        week_status = get_current_week_status()
        if not week_status.get("error"):
            ctx["current_plan_week"] = week_status.get("plan_week")
            ctx["remaining_this_week"] = week_status.get("remaining", [])
            ctx["week_status"] = week_status
    except Exception:
        pass

    return ctx


# ── Weight ────────────────────────────────────────────────────────────────────

@mcp.tool()
def log_weight(weight_kg: float, date_str: str = "") -> str:
    """Save a weight entry. date_str format: YYYY-MM-DD. Leave empty for today."""
    d = date_str if date_str else today_israel().isoformat()
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO weight (date, weight) VALUES (?, ?)", (d, weight_kg))
    conn.commit()
    conn.close()
    return f"נשמר: {weight_kg}ק״ג בתאריך {d}"


@mcp.tool()
def get_recent_weights(limit: int = 10) -> list:
    """Get the last N weight entries. Returns list of {date, weight_kg}."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT date, weight FROM weight ORDER BY date DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"date": r[0], "weight_kg": r[1]} for r in rows]


# ── Runs ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def log_run(date_str: str, distance_km: float, duration: str, pace: str,
            avg_heart_rate: int, feedback: str, workout_type: str = "") -> str:
    """
    Save a completed run to the database.
    Returns JSON with plan comparison info for the post-run review.
    plan_status: 'matched' | 'extra' | 'none'
    matched_type: which weekly slot was checked off (or null)
    remaining_this_week: list of workout types still open this week
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO runs (date, distance, duration, pace, avg_heart_rate, bot_feedback, workout_type) VALUES (?,?,?,?,?,?,?)",
        (date_str, distance_km, duration, pace, avg_heart_rate, feedback, workout_type)
    )
    conn.commit()

    result = {
        "saved": True,
        "plan_status": "none",
        "matched_type": None,
        "planned_km": None,
        "distance_diff": None,
        "plan_msg_he": "",
        "remaining_this_week": [],
    }

    try:
        current_week = _maybe_advance_week(conn)
        if current_week is not None:
            today = today_israel()
            calendar_week_start = _current_week_start(today).isoformat()

            c.execute(
                "SELECT workout_type, target_distance FROM weekly_plan WHERE week=? ORDER BY id",
                (current_week,)
            )
            planned_rows = c.fetchall()

            c.execute(
                "SELECT workout_type, COUNT(*) FROM weekly_execution WHERE calendar_week=? GROUP BY workout_type",
                (calendar_week_start,)
            )
            done_counts = {r[0]: r[1] for r in c.fetchall()}

            # Build unmatched list respecting multiple slots of the same type
            seen = {}
            unmatched = []
            for wtype, dist in planned_rows:
                seen[wtype] = seen.get(wtype, 0) + 1
                if seen[wtype] > done_counts.get(wtype, 0):
                    unmatched.append((wtype, dist))

            actual_type = (workout_type or "").strip()
            matched_slot = None
            matched_dist = None

            # Recovery Jog → Easy Run alias
            match_lookup = "Easy Run" if actual_type.lower() == "recovery jog" else actual_type

            for slot_type, slot_dist in unmatched:
                if slot_type.lower() == match_lookup.lower():
                    matched_slot = slot_type
                    matched_dist = slot_dist
                    break

            if matched_slot:
                dist_diff = round(distance_km - (matched_dist or 0), 2) if matched_dist else 0
                c.execute(
                    "INSERT OR IGNORE INTO weekly_execution "
                    "(plan_week, calendar_week, workout_type, run_date, actual_distance, actual_pace, distance_diff) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (current_week, calendar_week_start, matched_slot, date_str, distance_km, pace, dist_diff)
                )
                conn.commit()

                # Remove only the first occurrence of the matched slot
                _rem = list(unmatched)
                for _i, (_t, _) in enumerate(_rem):
                    if _t == matched_slot:
                        del _rem[_i]
                        break
                remaining = [t for t, _ in _rem]
                result["plan_status"] = "matched"
                result["matched_type"] = matched_slot
                result["planned_km"] = matched_dist
                result["distance_diff"] = dist_diff
                result["remaining_this_week"] = remaining
                if matched_dist:
                    diff_str = f"+{dist_diff}" if dist_diff >= 0 else str(dist_diff)
                    result["plan_msg_he"] = (
                        f"סומן: {matched_slot} שבוע {current_week} ({diff_str} ק״מ מהיעד)"
                    )
                else:
                    result["plan_msg_he"] = f"סומן: {matched_slot} שבוע {current_week}"
            elif actual_type:
                remaining = [t for t, _ in unmatched]
                result["plan_status"] = "extra"
                result["remaining_this_week"] = remaining
                result["plan_msg_he"] = "ריצה חופשית — לא מסמנת כלום בתכנית"
            else:
                result["remaining_this_week"] = [t for t, _ in unmatched]
    except Exception:
        pass

    conn.close()
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def get_recent_runs(limit: int = 10) -> list:
    """Get the last N runs. Returns list of {date, distance_km, pace, avg_heart_rate, feedback, workout_type}."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT date, distance, pace, avg_heart_rate, bot_feedback, workout_type FROM runs ORDER BY date DESC LIMIT ?",
        (limit,)
    )
    rows = c.fetchall()
    conn.close()
    return [{"date": r[0], "distance_km": r[1], "pace": r[2], "avg_heart_rate": r[3],
             "feedback": r[4], "workout_type": r[5]} for r in rows]


@mcp.tool()
def get_runs_by_type(workout_type: str, limit: int = 6) -> list:
    """Get recent runs filtered by workout type (Easy Run, Long Run, Tempo, Intervals)."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT date, distance, pace, avg_heart_rate FROM runs WHERE workout_type=? ORDER BY date DESC LIMIT ?",
        (workout_type, limit)
    )
    rows = c.fetchall()
    conn.close()
    return [{"date": r[0], "distance_km": r[1], "pace": r[2], "avg_heart_rate": r[3]} for r in rows]


@mcp.tool()
def get_hr_pace_analysis(limit: int = 20) -> dict:
    """
    Analyze the relationship between heart rate and pace across recent runs.
    Groups runs by workout type and computes:
    - Whether aerobic efficiency is improving (same pace, lower HR) or the runner is getting faster (same HR, faster pace)
    - HR and pace trend direction over time
    - A Hebrew summary per workout type and overall
    Call this when the user asks about fitness progress, HR trends, or whether they're getting fitter.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT date, workout_type, pace, avg_heart_rate FROM runs "
        "WHERE pace IS NOT NULL AND pace != '' AND avg_heart_rate IS NOT NULL AND avg_heart_rate > 0 "
        "ORDER BY date ASC LIMIT ?",
        (limit,)
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        return {"error": "אין נתוני ריצה עם קצב ודופק"}

    by_type: dict = {}
    for date_str, wtype, pace_str, hr in rows:
        key = wtype if wtype else "ריצה כללית"
        pace_secs = _pace_str_to_secs(pace_str)
        if pace_secs <= 0 or hr <= 0:
            continue
        by_type.setdefault(key, []).append({
            "date": date_str,
            "pace_secs": pace_secs,
            "pace_str": _secs_to_pace_str(pace_secs),
            "hr": hr,
            "efficiency": round(hr / pace_secs, 4),
        })

    results = {}
    summary_lines = []

    for wtype, runs in by_type.items():
        if len(runs) < 2:
            results[wtype] = {"runs": runs, "note_he": "רק ריצה אחת — אין מספיק נתונים להשוואה"}
            continue

        first_half = runs[:len(runs) // 2]
        second_half = runs[len(runs) // 2:]

        avg_pace_early = sum(r["pace_secs"] for r in first_half) / len(first_half)
        avg_pace_late  = sum(r["pace_secs"] for r in second_half) / len(second_half)
        avg_hr_early   = sum(r["hr"] for r in first_half) / len(first_half)
        avg_hr_late    = sum(r["hr"] for r in second_half) / len(second_half)

        pace_delta = avg_pace_late - avg_pace_early
        hr_delta   = avg_hr_late  - avg_hr_early

        faster = pace_delta < -5
        slower = pace_delta > 5
        lower_hr = hr_delta < -3
        higher_hr = hr_delta > 3

        if faster and lower_hr:
            trend = "מצוין"
            note = f"רצת {abs(int(pace_delta))} שניות יותר מהר וגם הדופק ירד ב־{abs(int(hr_delta))} פעימות — שיפור כושר ברור"
        elif faster and not higher_hr:
            trend = "טוב"
            note = f"קצב השתפר ב־{abs(int(pace_delta))} שניות לק״מ ללא עלייה בדופק"
        elif lower_hr and not slower:
            trend = "טוב"
            note = f"דופק ירד ב־{abs(int(hr_delta))} פעימות באותו קצב בערך — היכולת האירובית משתפרת"
        elif slower and higher_hr:
            trend = "לבדוק"
            note = f"קצב ירד ב־{abs(int(pace_delta))} שניות ודופק עלה ב־{abs(int(hr_delta))} פעימות — ייתכן עייפות או אימון יתר"
        elif higher_hr and not faster:
            trend = "לבדוק"
            note = f"דופק עלה ב־{abs(int(hr_delta))} פעימות ללא שיפור בקצב — שווה לבדוק מנוחה ושינה"
        else:
            trend = "יציב"
            note = "קצב ודופק יציבים — אפשר להוסיף עומס בהדרגה"

        results[wtype] = {
            "run_count": len(runs),
            "date_range": f"{runs[0]['date']} → {runs[-1]['date']}",
            "avg_pace_early": _secs_to_pace_str(int(avg_pace_early)),
            "avg_pace_late": _secs_to_pace_str(int(avg_pace_late)),
            "avg_hr_early": round(avg_hr_early),
            "avg_hr_late": round(avg_hr_late),
            "pace_change_sec": int(pace_delta),
            "hr_change_bpm": int(hr_delta),
            "trend": trend,
            "note_he": note,
            "runs": runs,
        }
        summary_lines.append(f"{wtype}: {note}")

    return {
        "by_type": results,
        "summary_he": " | ".join(summary_lines) if summary_lines else "אין מספיק נתונים",
    }


@mcp.tool()
def log_km_splits(run_date: str, splits: list) -> str:
    """
    Save per-km split data for a run.
    splits: list of {km, pace, distance_km} — pace as string e.g. '5:30'.
    Call this right after log_run whenever km_splits data is available.
    """
    def pace_to_seconds(p: str) -> int:
        try:
            m, s = p.replace("/km", "").strip().split(":")
            return int(m) * 60 + int(s)
        except Exception:
            return 0

    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM km_splits WHERE run_date=?", (run_date,))
    for split in splits:
        secs = pace_to_seconds(str(split.get("pace", "")))
        c.execute(
            "INSERT INTO km_splits (run_date, km, pace, pace_seconds, distance_km) VALUES (?,?,?,?,?)",
            (run_date, split.get("km"), split.get("pace"), secs, split.get("distance_km"))
        )
    conn.commit()
    conn.close()
    return f"נשמרו {len(splits)} ק״מ לתאריך {run_date}"


@mcp.tool()
def get_km_splits(run_date: str) -> list:
    """Get per-km splits for a specific run date. Returns list of {km, pace, pace_seconds, distance_km}."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT km, pace, pace_seconds, distance_km FROM km_splits WHERE run_date=? ORDER BY km",
        (run_date,)
    )
    rows = c.fetchall()
    conn.close()
    return [{"km": r[0], "pace": r[1], "pace_seconds": r[2], "distance_km": r[3]} for r in rows]


@mcp.tool()
def get_splits_history(limit: int = 5) -> list:
    """
    Get km splits for the last N runs that have split data stored.
    Returns list of {run_date, splits: [{km, pace, pace_seconds}]}.
    Use this to compare pace consistency and patterns across multiple runs.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT run_date FROM km_splits ORDER BY run_date DESC LIMIT ?", (limit,)
    )
    dates = [r[0] for r in c.fetchall()]
    result = []
    for d in dates:
        c.execute(
            "SELECT km, pace, pace_seconds FROM km_splits WHERE run_date=? ORDER BY km", (d,)
        )
        result.append({
            "run_date": d,
            "splits": [{"km": r[0], "pace": r[1], "pace_seconds": r[2]} for r in c.fetchall()]
        })
    conn.close()
    return result


# ── Nutrition ─────────────────────────────────────────────────────────────────

@mcp.tool()
def log_nutrition(meal_description: str, meal_type: str = "meal",
                  calories: int = 0, feedback: str = "", date_str: str = "") -> str:
    """Save a nutrition/meal entry."""
    d = date_str if date_str else today_israel().isoformat()
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO nutrition (date, meal_type, description, calories, feedback) VALUES (?,?,?,?,?)",
        (d, meal_type, meal_description, calories or None, feedback)
    )
    conn.commit()
    conn.close()
    return "ארוחה נשמרה"


@mcp.tool()
def get_recent_nutrition(limit: int = 10) -> list:
    """Get the last N nutrition entries."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT date, meal_type, description, calories FROM nutrition ORDER BY date DESC LIMIT ?",
        (limit,)
    )
    rows = c.fetchall()
    conn.close()
    return [{"date": r[0], "meal_type": r[1], "description": r[2], "calories": r[3]} for r in rows]


# ── Weekly Bucket Plan ────────────────────────────────────────────────────────

@mcp.tool()
def get_current_week_status() -> dict:
    """
    Returns the current plan week, what's planned, and what's been completed so far this calendar week.
    Auto-advances the week if Saturday has passed (lazy advancement).
    Call this when the user asks what's left this week, what to run today, or for plan status.
    """
    conn = get_connection()
    c = conn.cursor()

    current_week = _maybe_advance_week(conn)
    if current_week is None:
        conn.close()
        return {"error": "אין תכנית טעונה"}

    today = today_israel()
    calendar_week_start = _current_week_start(today).isoformat()
    week_saturday = (date.fromisoformat(calendar_week_start) + timedelta(days=6)).isoformat()

    c.execute(
        "SELECT workout_type, target_distance, notes FROM weekly_plan WHERE week=? ORDER BY id",
        (current_week,)
    )
    planned_rows = c.fetchall()

    c.execute(
        "SELECT workout_type, run_date, actual_distance FROM weekly_execution WHERE calendar_week=? ORDER BY id",
        (calendar_week_start,)
    )
    done_by_type: dict = {}
    for wtype, run_date, actual_dist in c.fetchall():
        done_by_type.setdefault(wtype, []).append({"run_date": run_date, "actual_distance": actual_dist})

    conn.close()

    if not planned_rows:
        return {"error": f"אין אימונים מוגדרים לשבוע {current_week} בתכנית"}

    planned = []
    slot_idx: dict = {}
    for wtype, target_dist, notes in planned_rows:
        idx = slot_idx.get(wtype, 0)
        slot_idx[wtype] = idx + 1
        done_list = done_by_type.get(wtype, [])
        done_info = done_list[idx] if idx < len(done_list) else None
        planned.append({
            "workout_type": wtype,
            "target_distance": target_dist,
            "notes": notes or "",
            "done": done_info is not None,
            "run_date": done_info["run_date"] if done_info else None,
            "actual_distance": done_info["actual_distance"] if done_info else None,
        })

    remaining = [p["workout_type"] for p in planned if not p["done"]]

    return {
        "plan_week": current_week,
        "calendar_week_start": calendar_week_start,
        "week_ends": week_saturday,
        "planned": planned,
        "completed_count": sum(1 for p in planned if p["done"]),
        "total_count": len(planned),
        "remaining": remaining,
    }


@mcp.tool()
def get_target_pace_for_type(workout_type: str) -> dict:
    """
    Compute a target pace for the given workout type based on recent runs and HR zones.
    workout_type: Easy Run, Long Run, Tempo, Intervals, Recovery Jog
    Returns {pace_range, zone, rationale_he}
    Cap: no more than 15s/km change from last run of same type.
    """
    runs = get_runs_by_type(workout_type, limit=5)
    zones_data = get_hr_zones()

    type_lower = workout_type.lower()
    if type_lower in ("easy run", "long run", "recovery jog"):
        target_zone = 2
    elif type_lower == "tempo":
        target_zone = 4
    elif type_lower == "intervals":
        target_zone = 5
    else:
        target_zone = 2

    zones_list = zones_data.get("zones", []) if "zones" in zones_data else []
    zone_info = next((z for z in zones_list if z["zone"] == target_zone), None)

    if not runs:
        # No history — fall back to zone-based estimate
        zone_paces = {1: (480, 510), 2: (390, 420), 3: (360, 390), 4: (300, 345), 5: (255, 300)}
        lo, hi = zone_paces.get(target_zone, (390, 420))
        return {
            "pace_range": f"{_secs_to_pace_str(lo)}-{_secs_to_pace_str(hi)}",
            "zone": target_zone,
            "rationale_he": f"אין היסטוריה של {workout_type} — הערכה לפי אזור {target_zone}",
        }

    secs_list = [_pace_str_to_secs(r["pace"]) for r in runs if r.get("pace")]
    secs_list = [s for s in secs_list if s > 0]

    if not secs_list:
        return {"pace_range": "לפי תחושה", "zone": target_zone, "rationale_he": "אין נתוני קצב"}

    avg_pace_secs = sum(secs_list) // len(secs_list)
    last_pace_secs = secs_list[0]

    # Check if HR was above target zone in recent runs
    if zone_info:
        recent_hrs = [r["avg_heart_rate"] for r in runs if r.get("avg_heart_rate") and r["avg_heart_rate"] > 0]
        avg_hr = sum(recent_hrs) / len(recent_hrs) if recent_hrs else 0
        if avg_hr > zone_info["max"] and target_zone in (1, 2):
            # Slow down — cap at +15s from last pace
            recommended = min(last_pace_secs + 15, avg_pace_secs + 30)
            pace_range = f"{_secs_to_pace_str(recommended)}-{_secs_to_pace_str(recommended + 15)}"
            return {
                "pace_range": pace_range,
                "zone": target_zone,
                "rationale_he": (
                    f"דופק ממוצע {int(avg_hr)} גבוה מאזור {target_zone} ({zone_info['max']} מקס׳) — "
                    f"האט ל-{pace_range}"
                ),
            }

    pace_range = f"{_secs_to_pace_str(avg_pace_secs)}-{_secs_to_pace_str(avg_pace_secs + 15)}"
    return {
        "pace_range": pace_range,
        "zone": target_zone,
        "rationale_he": f"ממוצע {len(secs_list)} ריצות אחרונות מסוג {workout_type}",
    }


@mcp.tool()
def save_training_plan(plan_text: str) -> str:
    """Parse a free-text training plan into weekly buckets and save. Replaces existing plan and resets to week 1."""
    client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "You are a data parser. Return JSON only, no extra text, no markdown."},
            {"role": "user", "content": (
                "Parse this training plan. Return a JSON array of weeks:\n"
                '[{"week": 1, "workouts": [{"workout_type": "Easy Run", "target_distance": 8, "notes": ""}, ...]}, ...]\n\n'
                "Rules:\n"
                "- week: integer (1-based)\n"
                "- workout_type must be exactly one of: Easy Run, Long Run, Tempo, Intervals\n"
                "- target_distance: total km as a number\n"
                "- notes: optional string (empty string if none)\n"
                "- Skip rest days. Return JSON array only.\n\n"
                f"Plan:\n{plan_text}"
            )}
        ]
    )

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.split("\n")[:-1])

    try:
        weeks_data = json.loads(raw)
    except Exception:
        return "לא הצלחתי לפרסר את התכנית — נסה לנסח מחדש"

    entries = []
    for week_obj in weeks_data:
        week_num = int(week_obj.get("week", 0))
        if not week_num:
            continue
        for workout in week_obj.get("workouts", []):
            wtype = workout.get("workout_type", "").strip()
            if wtype not in ("Easy Run", "Long Run", "Tempo", "Intervals"):
                continue
            dist = workout.get("target_distance")
            notes = workout.get("notes", "") or ""
            entries.append((week_num, wtype, float(dist) if dist else None, notes))

    if not entries:
        return "לא הצלחתי לפרסר את התכנית — נסה לנסח מחדש"

    today = today_israel()
    week_start = _current_week_start(today).isoformat()

    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM weekly_plan")
    c.execute("DELETE FROM weekly_execution")

    for week_num, wtype, dist, notes in entries:
        c.execute(
            "INSERT INTO weekly_plan (week, workout_type, target_distance, notes) VALUES (?,?,?,?)",
            (week_num, wtype, dist, notes)
        )

    c.execute("SELECT id FROM plan_week_position WHERE id=1")
    if c.fetchone():
        c.execute(
            "UPDATE plan_week_position SET current_week=1, week_start_date=? WHERE id=1",
            (week_start,)
        )
    else:
        c.execute(
            "INSERT INTO plan_week_position (id, current_week, week_start_date) VALUES (1, 1, ?)",
            (week_start,)
        )

    conn.commit()
    conn.close()

    num_weeks = len({e[0] for e in entries})
    return f"נשמרו {len(entries)} אימונים ל-{num_weeks} שבועות. מתחיל משבוע 1."


@mcp.tool()
def set_plan_position(week: int, run_num: int = 1) -> str:
    """Set the current plan week (e.g. week=2 to start week 2 this Sunday)."""
    today = today_israel()
    week_start = _current_week_start(today).isoformat()

    conn = get_connection()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM weekly_plan WHERE week=?", (week,))
    has_new_plan = c.fetchone()[0] > 0

    if not has_new_plan:
        # Fall back to old training_plan
        c.execute(
            "SELECT id FROM training_plan WHERE week=? AND day=? AND target_distance > 0",
            (week, str(run_num))
        )
        row = c.fetchone()
        if not row:
            conn.close()
            return f"לא מצאתי שבוע {week} בתכנית"
        # Old system: insert synthetic plan_execution to advance
        entries = []
        c.execute(
            "SELECT id FROM training_plan WHERE target_distance > 0 ORDER BY week ASC, id ASC"
        )
        entries = [r[0] for r in c.fetchall()]
        target_id = row[0]
        target_idx = entries.index(target_id) if target_id in entries else None
        if target_idx is None:
            conn.close()
            return f"לא מצאתי שבוע {week} ריצה {run_num} בתכנית"
        if target_idx == 0:
            c.execute("DELETE FROM plan_execution")
        else:
            prev_id = entries[target_idx - 1]
            c.execute(
                "INSERT INTO plan_execution (date, planned_workout, completed, skip_reason) VALUES (?,?,1,?)",
                (today.isoformat(), str(prev_id), "הוגדר ידנית")
            )
        conn.commit()
        conn.close()
        return f"הבא: שבוע {week} ריצה {run_num}"

    # New system: update plan_week_position
    c.execute("SELECT id FROM plan_week_position WHERE id=1")
    if c.fetchone():
        c.execute(
            "UPDATE plan_week_position SET current_week=?, week_start_date=? WHERE id=1",
            (week, week_start)
        )
    else:
        c.execute(
            "INSERT INTO plan_week_position (id, current_week, week_start_date) VALUES (1, ?, ?)",
            (week, week_start)
        )

    # Clear this week's execution so it starts fresh
    c.execute("DELETE FROM weekly_execution WHERE calendar_week >= ?", (week_start,))

    conn.commit()
    conn.close()
    return f"הבא: שבוע {week}"


# ── Legacy plan tools (kept for backward compat) ─────────────────────────────

@mcp.tool()
def get_training_plan() -> list:
    """Get the full training plan (legacy format)."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, week, day, workout_type, target_distance, target_pace, notes FROM training_plan ORDER BY week, id"
    )
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "week": r[1], "run_num": r[2], "workout_type": r[3],
             "target_distance": r[4], "target_pace": r[5], "notes": r[6]} for r in rows]


@mcp.tool()
def get_next_planned_workout() -> dict:
    """Get the next workout in the plan. Prefer get_current_week_status for new plans."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, week, day, workout_type, target_distance, target_pace, notes FROM training_plan WHERE target_distance > 0 ORDER BY week ASC, id ASC"
    )
    entries = c.fetchall()
    c.execute("SELECT planned_workout FROM plan_execution WHERE completed=1 ORDER BY date DESC LIMIT 1")
    last = c.fetchone()
    conn.close()

    if not entries:
        return {}

    if not last:
        e = entries[0]
    else:
        try:
            last_id = int(last[0])
            remaining = [e for e in entries if e[0] > last_id]
            e = remaining[0] if remaining else entries[0]
        except (ValueError, TypeError):
            e = entries[0]

    return {"id": e[0], "week": e[1], "run_num": e[2], "workout_type": e[3],
            "target_distance": e[4], "target_pace": e[5], "notes": e[6]}


@mcp.tool()
def log_plan_execution(planned_workout_id: str, completed: bool,
                       distance_diff: float = 0.0, pace_diff: str = "",
                       skip_reason: str = "") -> str:
    """Record whether a planned workout was completed (legacy)."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO plan_execution (date, planned_workout, completed, distance_diff, pace_diff, skip_reason) VALUES (?,?,?,?,?,?)",
        (today_israel().isoformat(), planned_workout_id, 1 if completed else 0,
         distance_diff, pace_diff, skip_reason)
    )
    conn.commit()
    conn.close()
    return "נשמר"


@mcp.tool()
def update_workout_paces(workout_type: str, new_pace: str) -> str:
    """
    Update the target pace for all future workouts of a given type in the training plan.
    Use this after analysing pace trends to make the plan harder or easier.
    workout_type: Easy Run / Long Run / Tempo / Intervals
    new_pace: pace string e.g. '5:30-5:50' or '5:10/km'
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "UPDATE training_plan SET target_pace=? WHERE workout_type=?",
        (new_pace, workout_type)
    )
    updated = c.rowcount
    conn.commit()
    conn.close()
    if updated == 0:
        return f"לא נמצאו אימוני {workout_type} בתכנית"
    return f"עודכן קצב יעד ל-{updated} אימוני {workout_type}: {new_pace}"


# ── HR Zones ──────────────────────────────────────────────────────────────────

@mcp.tool()
def get_hr_zones() -> dict:
    """
    Calculate heart rate training zones based on the user's max HR.
    If max_heart_rate is not set in profile, returns an estimate based on age (220 - age).
    Returns zones 1-5 with bpm ranges and what each zone means for training.
    Use this whenever analysing whether the user's run HR was appropriate for the workout type.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT max_heart_rate, age FROM user_profile WHERE id=1")
    row = c.fetchone()
    conn.close()

    max_hr = row[0] if row and row[0] else None
    age = row[1] if row and row[1] else None
    estimated = False

    if not max_hr:
        if age:
            max_hr = 220 - age
            estimated = True
        else:
            return {"error": "אין max_heart_rate או גיל בפרופיל. בקש מהמשתמש להזין את גילו."}

    zones = {
        "max_hr": max_hr,
        "estimated": estimated,
        "zones": [
            {"zone": 1, "name": "התאוששות",  "min": int(max_hr * 0.50), "max": int(max_hr * 0.60), "use": "חימום, קירור"},
            {"zone": 2, "name": "בסיס אירובי", "min": int(max_hr * 0.60), "max": int(max_hr * 0.70), "use": "ריצות קלות, בניית סיבולת"},
            {"zone": 3, "name": "אירובי",      "min": int(max_hr * 0.70), "max": int(max_hr * 0.80), "use": "ריצות נפח"},
            {"zone": 4, "name": "סף",          "min": int(max_hr * 0.80), "max": int(max_hr * 0.90), "use": "טמפו, ריצות סף"},
            {"zone": 5, "name": "מקסימום",     "min": int(max_hr * 0.90), "max": max_hr,             "use": "אינטרוולים"},
        ]
    }
    return zones


# ── Stats ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_weekly_stats() -> dict:
    """Get this week's runs, weight entries, nutrition logs, and plan progress."""
    today = today_israel()
    days_since_sunday = (today.weekday() + 1) % 7
    week_start = (today - timedelta(days=days_since_sunday)).isoformat()
    conn = get_connection()
    c = conn.cursor()

    c.execute("SELECT date, distance, pace, avg_heart_rate, workout_type FROM runs WHERE date >= ? ORDER BY date", (week_start,))
    runs = [{"date": r[0], "distance_km": r[1], "pace": r[2], "avg_heart_rate": r[3], "workout_type": r[4]}
            for r in c.fetchall()]

    c.execute("SELECT date, weight FROM weight WHERE date >= ? ORDER BY date", (week_start,))
    weights = [{"date": r[0], "weight_kg": r[1]} for r in c.fetchall()]

    c.execute("SELECT date, description, calories FROM nutrition WHERE date >= ? ORDER BY date", (week_start,))
    nutrition = [{"date": r[0], "description": r[1], "calories": r[2]} for r in c.fetchall()]

    conn.close()

    total_km = sum(r["distance_km"] for r in runs)

    return {
        "runs": runs,
        "total_km_this_week": round(total_km, 2),
        "weights": weights,
        "nutrition": nutrition,
        "week_start": week_start,
    }


@mcp.tool()
def recommend_next_workout() -> dict:
    """
    Recommend the next workout based on plan week status, days since last run, and recent intensity.
    Returns a structured recommendation in Hebrew.
    Call this whenever the user asks what to run, what's next, or is about to start a run.
    """
    runs = get_recent_runs(limit=5)
    week_status = get_current_week_status()

    days_since = 999
    last_run = None
    if runs:
        last_run = runs[0]
        try:
            last_date = date.fromisoformat(last_run["date"])
            days_since = (today_israel() - last_date).days
        except Exception:
            pass

    # Case 1: Long break — 4+ days since last run
    if days_since >= 4:
        next_type = "Easy Run"
        if not week_status.get("error") and week_status.get("remaining"):
            next_type = week_status["remaining"][0]
        pace_data = get_target_pace_for_type(next_type)
        dist = 5.0
        if not week_status.get("error"):
            for p in week_status.get("planned", []):
                if not p["done"] and p.get("target_distance"):
                    dist = round(p["target_distance"] * 0.80, 1)
                    break
        return {
            "days_since_last_run": days_since,
            "needs_recovery": False,
            "recommended": {
                "distance_km": dist,
                "pace_range": pace_data.get("pace_range", "לפי תחושה"),
                "workout_type": "Easy Run",
                "intensity_note": "קל, לא לדחוף",
            },
            "rationale_he": f"{days_since} ימים בלי ריצה — חזרה בקלילות, {dist} ק״מ בקצב נוח",
        }

    # Case 2: Recovery needed after hard effort
    hard_types = {"long run", "tempo", "intervals"}
    if days_since <= 1 and last_run and (last_run.get("workout_type") or "").lower() in hard_types:
        pace_data = get_target_pace_for_type("Easy Run")
        return {
            "days_since_last_run": days_since,
            "needs_recovery": True,
            "recommended": {
                "distance_km": 5.0,
                "pace_range": pace_data.get("pace_range", "לפי תחושה"),
                "workout_type": "Recovery Jog",
                "intensity_note": "התאוששות, מאוד קל",
            },
            "rationale_he": f"רצת {last_run.get('workout_type', 'ריצה קשה')} לאחרונה — מנוחה או 5 ק״מ התאוששות בלבד",
        }

    # Case 3: Follow the plan
    if not week_status.get("error") and week_status.get("remaining"):
        next_type = week_status["remaining"][0]
        target_dist = None
        for p in week_status.get("planned", []):
            if p["workout_type"] == next_type and not p["done"]:
                target_dist = p.get("target_distance")
                break

        pace_data = get_target_pace_for_type(next_type)

        return {
            "week_status": week_status,
            "days_since_last_run": days_since if days_since != 999 else None,
            "needs_recovery": False,
            "recommended": {
                "distance_km": target_dist,
                "pace_range": pace_data.get("pace_range", "לפי תחושה"),
                "workout_type": next_type,
                "intensity_note": pace_data.get("rationale_he", ""),
            },
            "rationale_he": (
                f"לפי תכנית שבוע {week_status['plan_week']}: {next_type}"
                + (f" {target_dist} ק״מ" if target_dist else "")
            ),
        }

    # Case 4: No plan or all done this week
    recent = runs[:3]
    avg_dist = round(sum(r["distance_km"] for r in recent) / len(recent), 1) if recent else 5.0
    pace_data = get_target_pace_for_type("Easy Run")
    return {
        "days_since_last_run": days_since if days_since != 999 else None,
        "needs_recovery": False,
        "recommended": {
            "distance_km": avg_dist,
            "pace_range": pace_data.get("pace_range", "לפי תחושה"),
            "workout_type": "Easy Run",
            "intensity_note": "קל, לפי תחושה",
        },
        "rationale_he": f"אין אימון פתוח בתכנית — ריצה קלה {avg_dist} ק״מ",
    }


if __name__ == "__main__":
    init_db()
    mcp.run()
