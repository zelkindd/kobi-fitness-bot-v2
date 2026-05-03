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


if __name__ == "__main__":
    mcp.run()
