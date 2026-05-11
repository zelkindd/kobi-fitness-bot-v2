import sqlite3
import os
import json
from datetime import date, timedelta
from fastmcp import FastMCP
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "kobi.db")
mcp = FastMCP("kobi-sqlite")

PLAN_MATCH_DISTANCE_TOLERANCE = 0.20

HEBREW_WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
HEBREW_MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
                 "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]


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
    """Return today's date, days since last run, last run summary, and current plan position."""
    today = date.today()
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

    c.execute("SELECT planned_workout FROM plan_execution WHERE completed=1 ORDER BY date DESC LIMIT 1")
    last_exec = c.fetchone()
    current_plan_position = None
    if last_exec:
        try:
            last_id = int(last_exec[0])
            c.execute(
                "SELECT week, day FROM training_plan WHERE id > ? AND target_distance > 0 ORDER BY week ASC, id ASC LIMIT 1",
                (last_id,)
            )
            next_row = c.fetchone()
            if next_row:
                current_plan_position = {"week": next_row[0], "run_num": next_row[1]}
        except Exception:
            pass

    conn.close()

    return {
        "today_iso": today.isoformat(),
        "today_hebrew": today_hebrew,
        "weekday": weekday_he,
        "days_since_last_run": days_since,
        "last_run": last_run,
        "current_plan_position": current_plan_position,
    }


# ── Weight ────────────────────────────────────────────────────────────────────

@mcp.tool()
def log_weight(weight_kg: float, date_str: str = "") -> str:
    """Save a weight entry. date_str format: YYYY-MM-DD. Leave empty for today."""
    d = date_str if date_str else date.today().isoformat()
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
    """Save a completed run to the database."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO runs (date, distance, duration, pace, avg_heart_rate, bot_feedback, workout_type) VALUES (?,?,?,?,?,?,?)",
        (date_str, distance_km, duration, pace, avg_heart_rate, feedback, workout_type)
    )
    conn.commit()

    plan_msg = ""
    try:
        planned = get_next_planned_workout()
        if planned and planned.get("target_distance"):
            planned_dist = planned["target_distance"]
            planned_type = (planned.get("workout_type") or "").lower()
            actual_type = (workout_type or "").lower()
            types_match = not planned_type or not actual_type or actual_type == planned_type
            dist_pct = abs(distance_km - planned_dist) / planned_dist
            if types_match and dist_pct <= PLAN_MATCH_DISTANCE_TOLERANCE:
                c.execute(
                    "INSERT INTO plan_execution (date, planned_workout, completed, distance_diff) VALUES (?,?,1,?)",
                    (date_str, str(planned["id"]), round(distance_km - planned_dist, 2))
                )
                conn.commit()
                plan_msg = f" סימנתי את שבוע {planned['week']} ריצה {planned['run_num']} כבוצע."
    except Exception:
        pass

    conn.close()
    return f"ריצה נשמרה.{plan_msg}" if plan_msg else "ריצה נשמרה בהצלחה"


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
    d = date_str if date_str else date.today().isoformat()
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


# ── Training Plan ─────────────────────────────────────────────────────────────

@mcp.tool()
def get_training_plan() -> list:
    """Get the full training plan."""
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
    """Get the next workout in the training plan based on last completed run."""
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
def recommend_next_workout() -> dict:
    """
    Recommend the next workout based on training plan position, days since last run,
    and recent workout intensity. Returns a structured recommendation in Hebrew.
    Call this whenever the user asks what to run, what's next, or is about to start a run.
    """
    runs = get_recent_runs(limit=5)
    planned = get_next_planned_workout()

    days_since = 999
    last_run = None
    if runs:
        last_run = runs[0]
        try:
            last_date = date.fromisoformat(last_run["date"])
            days_since = (date.today() - last_date).days
        except Exception:
            pass

    # Case 1: Long break — 4+ days since last run
    if days_since >= 4:
        if planned and planned.get("target_distance"):
            dist = round(planned["target_distance"] * 0.80, 1)
            pace_range = _make_easy_pace_range(planned.get("target_pace", ""), runs)
        else:
            recent = runs[:3]
            dist = round(sum(r["distance_km"] for r in recent) / len(recent) * 0.80, 1) if recent else 5.0
            pace_range = _make_easy_pace_range("", runs)
        return {
            "planned": planned if planned else None,
            "days_since_last_run": days_since,
            "needs_recovery": False,
            "recommended": {
                "distance_km": dist,
                "pace_range": pace_range,
                "workout_type": "Easy Run",
                "intensity_note": "קל, לא לדחוף",
            },
            "rationale_he": f"{days_since} ימים בלי ריצה — חזרה בקלילות, {dist} ק״מ בקצב נוח",
        }

    # Case 2: Ran today or yesterday after a hard effort
    hard_types = {"long run", "tempo", "intervals"}
    if days_since <= 1 and last_run and (last_run.get("workout_type") or "").lower() in hard_types:
        pace_range = _make_easy_pace_range("", runs)
        return {
            "planned": planned if planned else None,
            "days_since_last_run": days_since,
            "needs_recovery": True,
            "recommended": {
                "distance_km": 5.0,
                "pace_range": pace_range,
                "workout_type": "Recovery Jog",
                "intensity_note": "התאוששות, מאוד קל",
            },
            "rationale_he": f"רצת {last_run.get('workout_type', 'ריצה קשה')} לאחרונה — מנוחה או 5 ק״מ התאוששות בלבד",
        }

    # Case 3: Follow the plan
    if planned and planned.get("target_distance"):
        return {
            "planned": planned,
            "days_since_last_run": days_since if days_since != 999 else None,
            "needs_recovery": False,
            "recommended": {
                "distance_km": planned["target_distance"],
                "pace_range": planned.get("target_pace") or "לפי תחושה",
                "workout_type": planned.get("workout_type", "Easy Run"),
                "intensity_note": planned.get("notes") or "",
            },
            "rationale_he": (
                f"לפי תכנית: שבוע {planned.get('week')}, ריצה {planned.get('run_num')} — "
                f"{planned.get('workout_type')} {planned.get('target_distance')} ק״מ"
            ),
        }

    # Case 4: No plan loaded
    recent = runs[:3]
    avg_dist = round(sum(r["distance_km"] for r in recent) / len(recent), 1) if recent else 5.0
    pace_range = _make_easy_pace_range("", runs)
    return {
        "planned": None,
        "days_since_last_run": days_since if days_since != 999 else None,
        "needs_recovery": False,
        "recommended": {
            "distance_km": avg_dist,
            "pace_range": pace_range,
            "workout_type": "Easy Run",
            "intensity_note": "קל, לפי תחושה",
        },
        "rationale_he": f"אין תכנית טעונה — ריצה קלה {avg_dist} ק״מ בקצב הממוצע שלך",
    }


@mcp.tool()
def save_training_plan(plan_text: str) -> str:
    """Parse a free-text training plan and save it to the database. Replaces existing plan."""
    client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "You are a data parser. Extract training plan entries and return them in strict format only. No extra text."},
            {"role": "user", "content": (
                "Parse this training plan. Return each workout as:\n"
                "ENTRY: week|run_num|workout_type|distance_km|pace_zone|notes\n\n"
                "Rules:\n"
                "- week: integer (1-12)\n"
                "- run_num: 1, 2, 3, or 4\n"
                "- workout_type: Easy Run / Long Run / Tempo / Intervals\n"
                "- distance_km: total km\n"
                "- pace_zone: pace range string e.g. 6:20-6:40\n"
                "- notes: interval details or phase name\n"
                "- Skip rest days\n"
                "- One ENTRY line per workout, nothing else\n\n"
                f"Plan:\n{plan_text}"
            )}
        ]
    )

    entries = []
    for line in response.choices[0].message.content.splitlines():
        line = line.strip()
        if not line.startswith("ENTRY:"):
            continue
        try:
            parts = line[6:].strip().split("|")
            entries.append((int(parts[0]), parts[1].strip(), parts[2].strip(),
                            float(parts[3]), parts[4].strip(),
                            parts[5].strip() if len(parts) > 5 else ""))
        except Exception:
            continue

    if not entries:
        return "לא הצלחתי לפרסר את התכנית"

    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM training_plan")
    for week, run_num, workout_type, distance, pace, notes in entries:
        c.execute(
            "INSERT INTO training_plan (week, day, workout_type, target_distance, target_pace, notes) VALUES (?,?,?,?,?,?)",
            (week, run_num, workout_type, distance, pace, notes)
        )
    conn.commit()
    conn.close()
    return f"נשמרו {len(entries)} אימונים לתכנית"


@mcp.tool()
def set_plan_position(week: int, run_num: int) -> str:
    """Set which workout is next in the plan (e.g. week=2, run_num=1)."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id FROM training_plan WHERE target_distance > 0 ORDER BY week ASC, id ASC"
    )
    entries = [r[0] for r in c.fetchall()]

    target_idx = None
    c.execute(
        "SELECT id FROM training_plan WHERE week=? AND day=? AND target_distance > 0",
        (week, str(run_num))
    )
    row = c.fetchone()
    if row:
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
            (date.today().isoformat(), str(prev_id), "הוגדר ידנית")
        )
    conn.commit()
    conn.close()
    return f"הבא: שבוע {week} ריצה {run_num}"


@mcp.tool()
def log_plan_execution(planned_workout_id: str, completed: bool,
                       distance_diff: float = 0.0, pace_diff: str = "",
                       skip_reason: str = "") -> str:
    """Record whether a planned workout was completed."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO plan_execution (date, planned_workout, completed, distance_diff, pace_diff, skip_reason) VALUES (?,?,?,?,?,?)",
        (date.today().isoformat(), planned_workout_id, 1 if completed else 0,
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


@mcp.tool()
def get_weekly_stats() -> dict:
    """Get this week's runs, weight entries, nutrition logs, and plan progress."""
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    conn = get_connection()
    c = conn.cursor()

    c.execute("SELECT date, distance, pace, avg_heart_rate, workout_type FROM runs WHERE date >= ? ORDER BY date", (week_ago,))
    runs = [{"date": r[0], "distance_km": r[1], "pace": r[2], "avg_heart_rate": r[3], "workout_type": r[4]}
            for r in c.fetchall()]

    c.execute("SELECT date, weight FROM weight WHERE date >= ? ORDER BY date", (week_ago,))
    weights = [{"date": r[0], "weight_kg": r[1]} for r in c.fetchall()]

    c.execute("SELECT date, description, calories FROM nutrition WHERE date >= ? ORDER BY date", (week_ago,))
    nutrition = [{"date": r[0], "description": r[1], "calories": r[2]} for r in c.fetchall()]

    c.execute("SELECT planned_workout FROM plan_execution WHERE completed=1 ORDER BY date DESC LIMIT 1")
    last = c.fetchone()
    conn.close()

    total_km = sum(r["distance_km"] for r in runs)

    return {
        "runs": runs,
        "total_km_this_week": round(total_km, 2),
        "weights": weights,
        "nutrition": nutrition,
        "last_completed_plan_id": last[0] if last else None
    }


if __name__ == "__main__":
    init_db()
    mcp.run()
