import sqlite3
import os
import json
from datetime import date, timedelta
from fastmcp import FastMCP
import anthropic
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "kobi.db")
mcp = FastMCP("kobi-sqlite")


def get_connection():
    return sqlite3.connect(DB_PATH)


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
        weight_target REAL
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

    for col, coltype in [("weight_target", "REAL"), ("workout_type", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE user_profile ADD COLUMN {col} {coltype}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
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
    c.execute("SELECT * FROM user_profile WHERE id=1")
    row = c.fetchone()
    conn.close()
    if not row:
        return {}
    return {
        "bot_name": row[1],
        "user_name": row[2],
        "personality": row[3],
        "nutrition_rules": row[4],
        "weight_goal": row[5],
        "training_days": row[6],
        "weight_target": row[7] if len(row) > 7 else None,
    }


@mcp.tool()
def update_profile(field: str, value: str) -> str:
    """Update a single field in the user profile. Fields: user_name, personality, nutrition_rules, weight_goal, training_days, weight_target."""
    allowed = {"user_name", "personality", "nutrition_rules", "weight_goal", "training_days", "weight_target"}
    if field not in allowed:
        return f"שדה לא חוקי: {field}"
    conn = get_connection()
    c = conn.cursor()
    c.execute(f"UPDATE user_profile SET {field}=? WHERE id=1", (value,))
    conn.commit()
    conn.close()
    return "עודכן בהצלחה"


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
    conn.close()
    return "ריצה נשמרה בהצלחה"


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
def save_training_plan(plan_text: str) -> str:
    """Parse a free-text training plan and save it to the database. Replaces existing plan."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        system="You are a data parser. Extract training plan entries and return them in strict format only. No extra text.",
        messages=[{"role": "user", "content": (
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
        )}]
    )

    entries = []
    for line in response.content[0].text.splitlines():
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
