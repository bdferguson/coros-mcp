"""Export Coros activities to markdown + streams JSON files.

Reads activity summaries from the local SQLite cache, fetches full detail
(including time-series streams) from the Coros API, and writes files
compatible with the PersonalOS running activities format.

Output per activity:
  - {date}-{slug}.md       YAML frontmatter + summary line
  - {date}-{slug}.streams.json  Per-second HR, pace, cadence, altitude, distance
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from cache.store import get_activities, init_db
from cache.utils import LOCAL_TZ as _LOCAL_TZ
from coros_api import fetch_activity_detail, get_stored_auth, try_auto_login

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_DIR = Path.home() / "personal-os" / "areas" / "health" / "running" / "activities"
STATE_PATH = Path.home() / ".config" / "coros-mcp" / "export_state.json"

# Map Coros sport_type to Strava-compatible type strings
ACTIVITY_TYPE_MAP: dict[int, str] = {
    100: "Run",
    101: "Run",          # Indoor Running (treadmill flag set separately)
    102: "TrailRun",
    103: "Run",          # Track Running
    104: "Hike",
    200: "Ride",
    201: "Ride",         # Indoor Cycling
    203: "Ride",         # Gravel
    204: "Ride",         # MTB
    400: "Workout",      # Cardio
    402: "WeightTraining",
    403: "Yoga",
    900: "Walk",
    902: "Workout",      # Stair Climb
    903: "Workout",      # Elliptical
}

# Sport types considered treadmill/indoor
INDOOR_SPORT_TYPES = {101, 201}


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"exported_ids": []}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _unix_to_datetime(unix_str: str) -> datetime:
    """Convert Unix seconds string (from ActivitySummary.start_time) to local datetime."""
    ts = int(unix_str)
    # Distinguish seconds from milliseconds by magnitude (not string length)
    if ts > 1_000_000_000_000:
        ts = ts / 1000
    if _LOCAL_TZ is not None:
        return datetime.fromtimestamp(ts, tz=_LOCAL_TZ)
    return datetime.fromtimestamp(ts)


# ---------------------------------------------------------------------------
# File naming
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text[:60]


def _activity_filename(date_str: str, name: str) -> str:
    return f"{date_str}-{_slugify(name)}"


# ---------------------------------------------------------------------------
# Pace / speed helpers
# ---------------------------------------------------------------------------

def _sec_per_km_to_ms(spk: float) -> float:
    """Convert seconds-per-km to meters-per-second."""
    if not spk or spk <= 0:
        return 0.0
    return 1000.0 / spk


def _format_pace(spk: float) -> str:
    """Format seconds-per-km as M:SS string."""
    if not spk or spk <= 0:
        return "N/A"
    minutes = int(spk) // 60
    seconds = int(spk) % 60
    return f"{minutes}:{seconds:02d}"


# ---------------------------------------------------------------------------
# Stream extraction from frequencyList
# ---------------------------------------------------------------------------

def _extract_streams(frequency_list: list[dict]) -> dict:
    """Parse Coros frequencyList into Strava-compatible streams dict.

    Handles sparse fields by forward-filling from the last known value.
    """
    if not frequency_list:
        return {}

    first_ts = frequency_list[0].get("timestamp", 0)

    time_arr = []
    hr_arr = []
    speed_arr = []
    cadence_arr = []
    distance_arr = []
    altitude_arr = []

    last_speed = 0.0
    last_cadence = 0
    last_altitude = 0.0
    has_altitude = any("altitude" in e for e in frequency_list[:100])

    for entry in frequency_list:
        ts = entry.get("timestamp", first_ts)
        elapsed_sec = int(round((ts - first_ts) / 100.0))
        time_arr.append(elapsed_sec)

        # Heart rate (present in almost every entry)
        hr_arr.append(entry.get("heart", 0))

        # Speed (sec/km -> m/s), forward-fill if missing
        spk = entry.get("speed")
        if spk is not None and spk > 0:
            last_speed = _sec_per_km_to_ms(spk)
        speed_arr.append(round(last_speed, 2))

        # Cadence (spm, forward-fill)
        cad = entry.get("cadence")
        if cad is not None:
            last_cadence = cad
        cadence_arr.append(last_cadence)

        # Distance (cm -> m)
        dist_cm = entry.get("distance", 0)
        distance_arr.append(round(dist_cm / 100.0, 1))

        # Altitude (m, forward-fill, outdoor only)
        if has_altitude:
            alt = entry.get("altitude")
            if alt is not None:
                last_altitude = alt
            altitude_arr.append(last_altitude)

    n = len(time_arr)

    def _stream(data: list) -> dict:
        return {
            "data": data,
            "series_type": "distance",
            "original_size": n,
            "resolution": "high",
        }

    streams = {
        "time": _stream(time_arr),
        "heartrate": _stream(hr_arr),
        "velocity_smooth": _stream(speed_arr),
        "cadence": _stream(cadence_arr),
        "distance": _stream(distance_arr),
    }
    if has_altitude and altitude_arr:
        streams["altitude"] = _stream(altitude_arr)

    return streams


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------

def _write_frontmatter(data: dict) -> str:
    """Format a flat dict as YAML frontmatter (no pyyaml dependency)."""
    lines = ["---"]
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, str):
            # Escape single quotes by doubling them per YAML spec
            escaped = v.replace("'", "''")
            lines.append(f"{k}: '{escaped}'")
        elif isinstance(v, float):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _write_activity_files(
    activity_id: str,
    summary: dict,
    frequency_list: list[dict],
    sport_type: int,
    start_dt: datetime,
    output_dir: Path,
) -> tuple[Path | None, Path | None]:
    """Write markdown + streams.json files for one activity. Returns (md_path, streams_path)."""

    output_dir.mkdir(parents=True, exist_ok=True)

    name = summary.get("name", "Activity")
    date_str = start_dt.strftime("%Y-%m-%d")
    time_str = start_dt.strftime("%H:%M:%S")
    basename = _activity_filename(date_str, name)

    # Distance: cm -> km
    distance_cm = summary.get("distance", 0)
    distance_km = round(distance_cm / 100_000, 2)

    # Duration: centiseconds -> minutes
    total_cs = summary.get("totalTime", 0) or summary.get("workoutTime", 0)
    duration_min = round(total_cs / 6000, 1)
    workout_cs = summary.get("workoutTime", 0) or total_cs
    elapsed_min = round(workout_cs / 6000, 1)

    # Pace: avgMoveSpeed or avgSpeed in sec/km
    avg_spk = summary.get("avgMoveSpeed") or summary.get("avgSpeed", 0)
    max_spk = summary.get("maxSpeed", 0)
    # maxSpeed is the fastest pace (lowest sec/km value)

    # Calories: physical cal / 1000 -> kcal
    raw_cal = summary.get("calories", 0)
    calories = round(raw_cal / 1000, 1) if raw_cal else None

    activity_type = ACTIVITY_TYPE_MAP.get(sport_type, f"Sport{sport_type}")
    is_treadmill = sport_type in INDOOR_SPORT_TYPES

    frontmatter = {
        "activity_id": activity_id,
        "date": date_str,
        "time": time_str,
        "name": name,
        "type": activity_type,
        "distance_km": distance_km,
        "duration_min": duration_min,
        "elapsed_min": elapsed_min,
        "avg_pace": _format_pace(avg_spk),
        "max_pace": _format_pace(max_spk),
        "avg_hr": summary.get("avgHr") or None,
        "max_hr": summary.get("maxHr") or None,
        "avg_cadence": summary.get("avgCadence") or None,
        "elevation_gain": summary.get("elevGain", 0),
        "calories": calories,
        "device": "COROS PACE 3",
        "treadmill": is_treadmill,
    }

    # Build body
    parts = [f"{distance_km} km", f"{duration_min} min"]
    if summary.get("avgHr"):
        parts.append(f"avg HR {summary['avgHr']}")
    if summary.get("avgCadence"):
        parts.append(f"cadence {summary['avgCadence']}")
    body_line = " | ".join(parts)

    md_content = _write_frontmatter(frontmatter) + "\n\n" + body_line + "\n"
    md_path = output_dir / f"{basename}.md"
    md_path.write_text(md_content)

    # Streams
    streams_path = None
    if frequency_list:
        streams = _extract_streams(frequency_list)
        if streams:
            streams_data = {
                "activity_id": activity_id,
                "date": date_str,
                "name": name,
                "streams": streams,
            }
            streams_path = output_dir / f"{basename}.streams.json"
            streams_path.write_text(json.dumps(streams_data, indent=2) + "\n")

    return md_path, streams_path


# ---------------------------------------------------------------------------
# Main export logic
# ---------------------------------------------------------------------------

async def export_activities(
    auth,
    days: int = 7,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    full: bool = False,
) -> dict:
    """Export activities from Coros cache + API to markdown/streams files.

    Returns dict with counts: exported, skipped, errors.
    """
    init_db()

    # Determine date range from cache
    end_day = datetime.now().strftime("%Y%m%d")
    start_day = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    activities = get_activities(start_day, end_day)
    if not activities:
        return {"exported": 0, "skipped": 0, "errors": []}

    state = _load_state()
    exported_ids = set(state.get("exported_ids", []))

    exported = 0
    skipped = 0
    errors = []

    try:
        for act in activities:
            aid = act.activity_id

            if not full and aid in exported_ids:
                skipped += 1
                continue

            # Check if file already exists (by date + name slug)
            if act.start_time:
                start_dt = _unix_to_datetime(act.start_time)
            else:
                skipped += 1
                continue

            date_str = start_dt.strftime("%Y-%m-%d")
            name = act.name or "Activity"
            basename = _activity_filename(date_str, name)
            md_path = output_dir / f"{basename}.md"
            if md_path.exists() and not full:
                exported_ids.add(aid)
                skipped += 1
                continue

            sport_type = act.sport_type if act.sport_type is not None else 100
            try:
                detail = await fetch_activity_detail(
                    auth, aid, sport_type, include_streams=True,
                )
            except Exception as e:
                errors.append(f"{aid} ({name}): {e}")
                continue

            summary = detail.get("summary", {})
            frequency_list = detail.get("frequencyList", [])

            md, streams = _write_activity_files(
                activity_id=aid,
                summary=summary,
                frequency_list=frequency_list,
                sport_type=sport_type,
                start_dt=start_dt,
                output_dir=output_dir,
            )

            exported_ids.add(aid)
            exported += 1

            label = f"{date_str} {name}"
            streams_note = f" + streams ({len(frequency_list)} pts)" if streams else ""
            print(f"  Exported: {label}{streams_note}")

            # Save state after each successful export (crash recovery)
            state["exported_ids"] = sorted(exported_ids)
            _save_state(state)

            # Small delay between API calls to be respectful
            await asyncio.sleep(0.3)
    finally:
        # Ensure state is saved even if we crash mid-loop
        state["exported_ids"] = sorted(exported_ids)
        _save_state(state)

    return {"exported": exported, "skipped": skipped, "errors": errors}
