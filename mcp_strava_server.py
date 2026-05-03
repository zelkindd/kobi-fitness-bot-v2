import os
import requests
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN")

mcp = FastMCP("kobi-strava")


def get_access_token() -> str:
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token"
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


@mcp.tool()
def get_latest_activity() -> dict:
    """
    Fetch the most recent run from Strava.
    Returns distance_km, duration, pace, avg_heart_rate, name, date, and per-km splits.
    Call this whenever the user says they finished a run or asks about their latest run.
    """
    try:
        token = get_access_token()
    except Exception as e:
        return {"error": f"לא הצלחתי להתחבר לסטרבה: {e}"}

    resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers={"Authorization": f"Bearer {token}"},
        params={"per_page": 5}
    )
    resp.raise_for_status()
    activities = resp.json()

    for activity in activities:
        if activity.get("type") == "Run" or activity.get("sport_type") == "Run":
            distance_km = round(activity["distance"] / 1000, 2)
            moving_time = activity["moving_time"]
            duration = f"{moving_time // 60}:{moving_time % 60:02d}"

            if activity["distance"] > 0:
                pace_sec = moving_time / (activity["distance"] / 1000)
                pace = f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d}/km"
            else:
                pace = "לא ידוע"

            avg_hr = activity.get("average_heartrate")

            km_paces = []
            try:
                detail = requests.get(
                    f"https://www.strava.com/api/v3/activities/{activity['id']}",
                    headers={"Authorization": f"Bearer {token}"}
                ).json()
                for split in detail.get("splits_metric", []):
                    elapsed = split.get("moving_time", 0)
                    dist = split.get("distance", 0)
                    if dist > 0 and elapsed > 0:
                        spm = elapsed / (dist / 1000)
                        km_paces.append({
                            "km": split.get("split", len(km_paces) + 1),
                            "pace": f"{int(spm // 60)}:{int(spm % 60):02d}",
                            "distance_km": round(dist / 1000, 3)
                        })
            except Exception:
                km_paces = []

            return {
                "distance_km": distance_km,
                "duration": duration,
                "pace": pace,
                "avg_heart_rate": int(avg_hr) if avg_hr else None,
                "name": activity.get("name", "ריצה"),
                "date": activity["start_date_local"][:10],
                "km_splits": km_paces
            }

    return {"error": "לא נמצאה ריצה אחרונה בסטרבה"}


@mcp.tool()
def estimate_max_hr_from_strava() -> dict:
    """
    Scan the last 100 Strava activities and return the highest recorded heart rate.
    This is a real-world estimate of the user's max HR — more accurate than a formula.
    Call this when the user asks about max HR or when get_hr_zones returns an error.
    """
    try:
        token = get_access_token()
    except Exception as e:
        return {"error": f"לא הצלחתי להתחבר לסטרבה: {e}"}

    resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers={"Authorization": f"Bearer {token}"},
        params={"per_page": 100}
    )
    resp.raise_for_status()
    activities = resp.json()

    max_hr = 0
    max_hr_activity = None
    for a in activities:
        hr = a.get("max_heartrate") or 0
        if hr > max_hr:
            max_hr = hr
            max_hr_activity = {"date": a["start_date_local"][:10], "name": a.get("name", "")}

    if max_hr == 0:
        return {"error": "לא נמצאו נתוני דופק בפעילויות האחרונות"}

    return {
        "max_hr_recorded": max_hr,
        "from_activity": max_hr_activity,
        "note": "זהו הדופק המקסימלי שנרשם בסטרבה. כדאי לשמור אותו בפרופיל עם update_profile(max_heart_rate)."
    }


def _parse_splits(detail: dict) -> list:
    splits = []
    for split in detail.get("splits_metric", []):
        elapsed = split.get("moving_time", 0)
        dist = split.get("distance", 0)
        if dist > 0 and elapsed > 0:
            spm = elapsed / (dist / 1000)
            splits.append({
                "km": split.get("split", len(splits) + 1),
                "pace": f"{int(spm // 60)}:{int(spm % 60):02d}",
                "distance_km": round(dist / 1000, 3)
            })
    return splits


@mcp.tool()
def get_recent_activities_with_splits(limit: int = 5) -> list:
    """
    Fetch the last N runs from Strava, each with per-km splits.
    Use this to analyse pace patterns across multiple past runs.
    Returns list of {date, distance_km, pace, avg_heart_rate, km_splits}.
    """
    try:
        token = get_access_token()
    except Exception as e:
        return [{"error": f"לא הצלחתי להתחבר לסטרבה: {e}"}]

    resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers={"Authorization": f"Bearer {token}"},
        params={"per_page": 20}
    )
    resp.raise_for_status()
    activities = resp.json()

    runs = []
    for activity in activities:
        if len(runs) >= limit:
            break
        if activity.get("type") != "Run" and activity.get("sport_type") != "Run":
            continue

        moving_time = activity["moving_time"]
        dist = activity["distance"]
        pace_sec = moving_time / (dist / 1000) if dist > 0 else 0

        try:
            detail = requests.get(
                f"https://www.strava.com/api/v3/activities/{activity['id']}",
                headers={"Authorization": f"Bearer {token}"}
            ).json()
            km_splits = _parse_splits(detail)
        except Exception:
            km_splits = []

        runs.append({
            "date": activity["start_date_local"][:10],
            "distance_km": round(dist / 1000, 2),
            "pace": f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d}/km",
            "avg_heart_rate": int(activity["average_heartrate"]) if activity.get("average_heartrate") else None,
            "km_splits": km_splits
        })

    return runs


if __name__ == "__main__":
    mcp.run()
