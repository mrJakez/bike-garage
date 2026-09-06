from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import secrets
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .connectors.strava_proxy import fetch_bikes as fetch_strava_bikes, health_check, normalise_base_url, test_connection, update_activity_gear as update_strava_activity_gear
from .connectors.hammerhead import DEFAULT_API_BASE_URL, authorization_url as hammerhead_authorization_url, exchange_code as exchange_hammerhead_code, fetch_activity_detail as fetch_hammerhead_activity_detail, fetch_activity_fit as fetch_hammerhead_activity_fit, normalise_base_url as normalise_hammerhead_base_url, test_connection as test_hammerhead_connection
from .database import connection, initial_user, initialise_database
from .sync import ActivityContext, apply_rule, attach_hardware_observations, expression_error, evaluate_expression, refresh_all_canonical_timings, repair_strava_local_start_times, start_sync_loop, store_fit_hardware, sync_all_connections, validate_expression


APP_ROOT = Path(__file__).parent
PHOTO_ROOT = Path(os.getenv("DATABASE_PATH", "data/bike-garage.db")).parent / "bike-photos"
PHOTO_ROOT.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(APP_ROOT / "templates"))
LOCAL_TIMEZONE = ZoneInfo("Europe/Berlin")


def format_activity_datetime(value: object) -> str:
    """Present provider timestamps without exposing ISO/UTC implementation detail."""
    if not value:
        return "Unknown date"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Form values such as the Garage start are intentionally stored as
        # local, timezone-naive values. Provider values carry their UTC offset.
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(LOCAL_TIMEZONE)
        return parsed.strftime("%d.%m.%Y · %H:%M")
    except (TypeError, ValueError):
        return str(value)


def format_log_datetime(value: object) -> str:
    """Render SQLite CURRENT_TIMESTAMP values (UTC) in the garage timezone."""
    if not value:
        return "Unknown date"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y · %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


def format_kilometres(value: object, decimals: int = 1) -> str:
    """Format mileage for the German UI: 10.182,6 instead of 10182.6."""
    try:
        numeric = float(value or 0)
        precision = max(0, int(decimals))
    except (TypeError, ValueError):
        return "—"
    # Python formats thousands with commas and decimals with a dot. Swap the
    # separators without relying on the process locale (which is not stable in
    # Docker images).
    return f"{numeric:,.{precision}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def attach_bike_mileage(db: object, bikes: list[dict[str, object]], user_id: int) -> dict[str, object]:
    """Attach the baseline plus eligible assigned-activity distance to bikes."""
    setting_row = db.execute(
        "SELECT mileage_tracking_started_at, mileage_tracking_started_at_epoch FROM user_settings WHERE user_id=?",
        (user_id,),
    ).fetchone()
    settings = dict(setting_row) if setting_row else {
        "mileage_tracking_started_at": None, "mileage_tracking_started_at_epoch": None,
    }
    started_epoch = settings.get("mileage_tracking_started_at_epoch")
    tracked_by_bike: dict[int, float] = {}
    if started_epoch is not None:
        for row in db.execute(
            """SELECT aba.bike_id, COALESCE(SUM(a.distance_m), 0) AS distance_m
               FROM activity_bike_assignments aba
               JOIN activities a ON a.id=aba.activity_id
               WHERE a.started_at_epoch >= ? AND a.deleted_at IS NULL
               GROUP BY aba.bike_id""",
            (started_epoch,),
        ).fetchall():
            tracked_by_bike[int(row["bike_id"])] = float(row["distance_m"] or 0)
    for bike in bikes:
        baseline = float(bike.get("starting_mileage_m") or 0)
        tracked = tracked_by_bike.get(int(bike["id"]), 0)
        bike["activity_mileage_m"] = tracked
        bike["total_mileage_m"] = baseline + tracked
        bike["mileage_tracking_active"] = started_epoch is not None
    return settings


templates.env.filters["activity_datetime"] = format_activity_datetime
templates.env.filters["log_datetime"] = format_log_datetime
templates.env.filters["km"] = format_kilometres
app = FastAPI(title="Bike Garage")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")

PROVIDERS = {
    "STRAVA_PROXY": {
        "name": "Strava Proxy",
        "description": "Your own local service that owns Strava OAuth and exposes activities per account identifier.",
        "connection_type": "proxy",
        "icon": "strava",
    },
    "HAMMERHEAD": {
        "name": "Hammerhead",
        "description": "Read Karoo activities directly from the Hammerhead API using an OAuth token with activity:read.",
        "connection_type": "oauth_token",
        "icon": "hammerhead",
    },
}

# Strava's current SportType enum (the value exposed on an activity).
STRAVA_ACTIVITY_TYPES = (
    "Ride", "GravelRide", "MountainBikeRide", "EMountainBikeRide", "EBikeRide", "VirtualRide",
    "Run", "TrailRun", "VirtualRun", "Walk", "Hike", "AlpineSki", "BackcountrySki", "NordicSki",
    "RollerSki", "Snowboard", "Snowshoe", "Swim", "Canoeing", "Kayaking", "Rowing", "VirtualRow",
    "StandUpPaddling", "Surfing", "Sail", "IceSkate", "InlineSkate", "Skateboard", "Wheelchair",
    "Handcycle", "Golf", "Tennis", "Badminton", "Pickleball", "Racquetball", "Squash", "TableTennis",
    "Soccer", "Basketball", "Volleyball", "Cricket", "RockClimbing", "Elliptical", "StairStepper",
    "WeightTraining", "Crossfit", "HighIntensityIntervalTraining", "Pilates", "Yoga", "Dance",
    "Workout", "PhysicalTherapy", "Kitesurf", "Velomobile",
)

BIKE_TYPES = ("Road", "Gravel", "Mountainbike")
BIKE_COMPONENT_TYPES = (
    ("shifting", "Electronic shifting"),
    ("bike_power", "Bike power / crank"),
    ("seatpost", "Seatpost"),
)
CONNECTION_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def connection_identifier(value: str) -> str:
    return value.strip().lower()


def rider_name_for_connection(display_name: object) -> str:
    """Derive a stable, human-facing rider label from an existing connection name.

    Connections currently hold the account label (e.g. ``Dennis Strava`` or
    ``Jakez via Hammerhead``), not a separate rider table. Keeping this small
    normalisation here lets one rider cover several provider connections.
    """
    label = str(display_name or "").strip()
    if " via " in label.lower():
        return re.split(r"\s+via\s+", label, flags=re.IGNORECASE)[0].strip() or label
    for suffix in (" Strava", " Hammerhead", " Proxy"):
        if label.lower().endswith(suffix.lower()):
            return label[: -len(suffix)].strip() or label
    return label


def component_ant_ids_from_form(values: dict[str, str | None]) -> dict[str, int]:
    """Validate the small V1 component catalogue submitted by the bike form."""
    result: dict[str, int] = {}
    for component_type, _ in BIKE_COMPONENT_TYPES:
        value = (values.get(f"{component_type}_ant_device_number") or "").strip()
        if not value:
            continue
        try:
            ant_number = int(value)
        except ValueError as error:
            raise ValueError(f"{component_type.replace('_', ' ')} ANT+ device number must be a whole number.") from error
        if ant_number <= 0 or ant_number > 65535:
            raise ValueError(f"{component_type.replace('_', ' ')} ANT+ device number must be between 1 and 65535.")
        result[component_type] = ant_number
    return result


def ensure_component_ids_available(db: object, component_ant_ids: dict[str, int], *, exclude_bike_id: int | None = None) -> None:
    for component_type, ant_number in component_ant_ids.items():
        query = "SELECT b.name FROM bike_components bc JOIN bikes b ON b.id=bc.bike_id WHERE bc.component_type=? AND bc.ant_device_number=?"
        values: tuple[object, ...] = (component_type, ant_number)
        if exclude_bike_id is not None:
            query += " AND bc.bike_id<>?"
            values += (exclude_bike_id,)
        owner = db.execute(
            query, values,
        ).fetchone()
        if owner is not None:
            raise ValueError(f"ANT+ ID {ant_number} for {component_type.replace('_', ' ')} is already assigned to {owner['name']}.")


def save_bike_components(db: object, bike_id: int, component_ant_ids: dict[str, int]) -> None:
    ensure_component_ids_available(db, component_ant_ids, exclude_bike_id=bike_id)
    db.execute("DELETE FROM bike_components WHERE bike_id=?", (bike_id,))
    for component_type, ant_number in component_ant_ids.items():
        db.execute(
            "INSERT INTO bike_components (bike_id,component_type,ant_device_number) VALUES (?,?,?)",
            (bike_id, component_type, ant_number),
        )


def bike_component_values(db: object, bike_id: int) -> dict[str, int]:
    return {
        row["component_type"]: row["ant_device_number"]
        for row in db.execute("SELECT component_type,ant_device_number FROM bike_components WHERE bike_id=?", (bike_id,)).fetchall()
    }


def refresh_strava_gear_catalog(db: object, user_id: int) -> list[str]:
    """Fetch the bike catalogue behind every connected Strava Proxy account."""
    connections = db.execute(
        """SELECT * FROM provider_connections
           WHERE user_id=? AND provider_type='STRAVA_PROXY' AND status='CONNECTED'""",
        (user_id,),
    ).fetchall()
    errors: list[str] = []
    def fetch(connection_row: object) -> tuple[object, list[dict] | None, Exception | None]:
        try:
            gears = fetch_strava_bikes(
                connection_row["endpoint_url"], connection_row["access_token"],
                connection_row["external_account_id"], timeout_seconds=4,
            )
        except Exception as error:  # The form remains usable with the last cached gear list.
            return connection_row, None, error
        return connection_row, gears, None

    # One unavailable local proxy must not make the bike form wait for every
    # other account. Fetch independent accounts in parallel and retain cache.
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(connections)))) as executor:
        future_map = {executor.submit(fetch, provider_connection): provider_connection for provider_connection in connections}
        results = [future.result() for future in as_completed(future_map)]
    for provider_connection, gears, error in results:
        if error is not None:
            errors.append(f"{provider_connection['display_name']}: {error}")
            continue
        assert gears is not None
        for gear in gears:
            gear_id = str(gear.get("id") or "").strip()
            if not gear_id:
                continue
            name = str(gear.get("name") or gear_id).strip()
            db.execute(
                """INSERT INTO strava_gears
                   (provider_connection_id,external_gear_id,name,distance_m,is_primary,raw_json,synced_at)
                   VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(provider_connection_id,external_gear_id) DO UPDATE SET
                     name=excluded.name,distance_m=excluded.distance_m,is_primary=excluded.is_primary,
                     raw_json=excluded.raw_json,synced_at=CURRENT_TIMESTAMP""",
                (
                    provider_connection["id"], gear_id, name, float(gear.get("distance") or 0),
                    int(bool(gear.get("primary"))), json.dumps(gear),
                ),
            )
    return errors


def strava_gear_options(db: object, bike_id: int | None = None) -> list[dict[str, object]]:
    """Return cached gear choices, including mappings to the bike being edited."""
    rows = db.execute(
        """SELECT sg.*, pc.display_name AS connection_name, pc.identifier AS connection_identifier,
                  current_map.bike_id AS mapped_bike_id, mapped_bike.name AS mapped_bike_name
           FROM strava_gears sg
           JOIN provider_connections pc ON pc.id=sg.provider_connection_id
           LEFT JOIN bike_strava_gear_mappings current_map ON current_map.strava_gear_id=sg.id
           LEFT JOIN bikes mapped_bike ON mapped_bike.id=current_map.bike_id
           ORDER BY pc.display_name COLLATE NOCASE, sg.is_primary DESC, sg.name COLLATE NOCASE"""
    ).fetchall()
    options: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        item["selected"] = bike_id is not None and item.get("mapped_bike_id") == bike_id
        item["available"] = item.get("mapped_bike_id") is None or item["selected"]
        options.append(item)
    return options


def validate_bike_strava_gears(db: object, bike_id: int | None, gear_ids: list[str]) -> set[int]:
    selected = {int(value) for value in gear_ids if str(value).isdigit()}
    if selected:
        placeholders = ",".join("?" for _ in selected)
        existing = db.execute(
            f"SELECT id FROM strava_gears WHERE id IN ({placeholders})", tuple(selected)
        ).fetchall()
        if {row["id"] for row in existing} != selected:
            raise ValueError("One or more selected Strava bikes are no longer available.")
        conflicts = db.execute(
            f"""SELECT b.name AS bike_name, sg.name AS gear_name FROM bike_strava_gear_mappings mapping
                JOIN bikes b ON b.id=mapping.bike_id JOIN strava_gears sg ON sg.id=mapping.strava_gear_id
                WHERE mapping.strava_gear_id IN ({placeholders}) AND (? IS NULL OR mapping.bike_id<>?)""",
            (*selected, bike_id, bike_id),
        ).fetchall()
        if conflicts:
            raise ValueError(f"Strava bike ‘{conflicts[0]['gear_name']}’ is already linked to {conflicts[0]['bike_name']}.")
    return selected


def save_bike_strava_gears(db: object, bike_id: int, gear_ids: list[str]) -> None:
    selected = validate_bike_strava_gears(db, bike_id, gear_ids)
    db.execute("DELETE FROM bike_strava_gear_mappings WHERE bike_id=?", (bike_id,))
    for gear_id in selected:
        db.execute(
            "INSERT INTO bike_strava_gear_mappings (bike_id,strava_gear_id) VALUES (?,?)",
            (bike_id, gear_id),
        )


def strava_gear_mismatches(db: object, activity_id: int) -> list[dict[str, object]]:
    """Return Strava sources whose configured gear differs from the resolved bike."""
    activity = db.execute(
        "SELECT started_at_epoch FROM activities WHERE id=? AND deleted_at IS NULL", (activity_id,)
    ).fetchone()
    if activity is None:
        return []
    settings = db.execute(
        "SELECT mileage_tracking_started_at_epoch FROM user_settings WHERE user_id=?", (initial_user()["id"],)
    ).fetchone()
    garage_start_epoch = settings["mileage_tracking_started_at_epoch"] if settings else None
    rows = db.execute(
        """SELECT pa.id AS provider_activity_id, pa.external_activity_id, pa.raw_json,
                  pc.id AS connection_id, pc.display_name AS connection_name, pc.endpoint_url,
                  pc.access_token, pc.external_account_id
           FROM activity_provider_links apl
           JOIN provider_activities pa ON pa.id=apl.provider_activity_id
           JOIN provider_connections pc ON pc.id=pa.connection_id
           WHERE apl.activity_id=? AND pc.provider_type='STRAVA_PROXY'""",
        (activity_id,),
    ).fetchall()
    mismatches: list[dict[str, object]] = []
    for row in rows:
        mapping = db.execute(
            """SELECT sg.external_gear_id, sg.name FROM activity_bike_assignments aba
               JOIN bike_strava_gear_mappings mapping ON mapping.bike_id=aba.bike_id
               JOIN strava_gears sg ON sg.id=mapping.strava_gear_id
               WHERE aba.activity_id=? AND sg.provider_connection_id=?
               ORDER BY aba.slot_index LIMIT 1""",
            (activity_id, row["connection_id"]),
        ).fetchone()
        if mapping is None:
            continue
        try:
            payload = json.loads(row["raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        current_gear_id = str(payload.get("gear_id") or "")
        if current_gear_id == mapping["external_gear_id"]:
            continue
        current_gear = db.execute(
            "SELECT name FROM strava_gears WHERE provider_connection_id=? AND external_gear_id=?",
            (row["connection_id"], current_gear_id),
        ).fetchone() if current_gear_id else None
        mismatches.append({
            **dict(row),
            "desired_gear_id": mapping["external_gear_id"],
            "desired_gear_name": mapping["name"],
            "current_gear_id": current_gear_id,
            "current_gear_name": current_gear["name"] if current_gear else (current_gear_id or "No bike"),
            "update_allowed": garage_start_epoch is not None and (activity["started_at_epoch"] or 0) >= garage_start_epoch,
        })
    return mismatches


def sync_strava_activity_gears(db: object, activity_id: int, provider_activity_id: int | None = None) -> tuple[int, int]:
    """Explicitly mirror a resolved bike to Strava after a user confirms it."""
    updated_count = 0
    skipped_count = 0
    for row in strava_gear_mismatches(db, activity_id):
        if provider_activity_id is not None and row["provider_activity_id"] != provider_activity_id:
            continue
        if not row["update_allowed"]:
            skipped_count += 1
            record_activity_log(
                db, activity_id,
                f"Strava bike update skipped for {row['connection_name']}: activity is before Garage start.",
                logger="STRAVA",
            )
            continue
        try:
            updated = update_strava_activity_gear(
                row["endpoint_url"], row["access_token"], row["external_account_id"],
                row["external_activity_id"], row["desired_gear_id"],
            )
        except Exception as error:
            record_activity_log(
                db, activity_id,
                f"Strava gear update failed for {row['connection_name']}: {error}",
                logger="STRAVA",
            )
            continue
        try:
            payload = json.loads(row["raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        payload.update(updated)
        payload["gear_id"] = row["desired_gear_id"]
        db.execute("UPDATE provider_activities SET raw_json=? WHERE id=?", (json.dumps(payload), row["provider_activity_id"]))
        old = row["current_gear_name"]
        record_activity_log(
            db, activity_id,
            f"Strava bike updated for {row['connection_name']}: {old} → {row['desired_gear_name']}.",
            logger="STRAVA",
        )
        updated_count += 1
    return updated_count, skipped_count


def connection_identifier_is_available(db: object, identifier: str, *, exclude_id: int | None = None) -> bool:
    if not CONNECTION_IDENTIFIER_PATTERN.fullmatch(identifier):
        return False
    query = "SELECT 1 FROM provider_connections WHERE identifier = ?"
    values: tuple[object, ...] = (identifier,)
    if exclude_id is not None:
        query += " AND id <> ?"
        values += (exclude_id,)
    return db.execute(query, values).fetchone() is None


def duration_label(value: object, *, milliseconds: bool = False) -> str:
    try:
        seconds = int(float(value) / 1000) if milliseconds else int(float(value))
    except (TypeError, ValueError):
        return "—"
    return str(timedelta(seconds=max(0, seconds)))


def record_activity_log(db: object, activity_id: int, message: str, *, logger: str = "UI") -> None:
    """Persist a user-visible, canonical activity log entry."""
    db.execute(
        "INSERT INTO activity_log_entries (activity_id,logger,message) VALUES (?,?,?)",
        (activity_id, logger, message),
    )


@app.on_event("startup")
def startup() -> None:
    initialise_database()
    # Older Strava Proxy imports stored start_date_local with a UTC suffix.
    # Normalise once at startup before pages render canonical activity times.
    if repair_strava_local_start_times():
        refresh_all_canonical_timings()
    start_sync_loop()


def page_context(request: Request, *, activity_filters: dict[str, object] | None = None, **extra: object) -> dict[str, object]:
    user = initial_user()
    with connection() as db:
        saved_connections = db.execute(
            "SELECT * FROM provider_connections WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
        ).fetchall()
    connections = []
    for item in saved_connections:
        connection_item = dict(item)
        try:
            connection_item["activity_types"] = json.loads(connection_item.get("activity_types_json") or "[]")
        except json.JSONDecodeError:
            connection_item["activity_types"] = []
        try:
            connection_item["last_test_activities"] = json.loads(connection_item.get("last_test_activities_json") or "[]")
        except json.JSONDecodeError:
            connection_item["last_test_activities"] = []
        connections.append(connection_item)
    with connection() as db:
        filters = dict(activity_filters or {})
        tracking_settings = db.execute(
            "SELECT mileage_tracking_started_at, mileage_tracking_started_at_epoch FROM user_settings WHERE user_id=?",
            (user["id"],),
        ).fetchone()
        garage_start_epoch = (
            tracking_settings["mileage_tracking_started_at_epoch"]
            if tracking_settings is not None
            else None
        )
        filters["garage_start_epoch"] = garage_start_epoch
        clauses: list[str] = []
        values: list[object] = []
        # Imported history remains available in the database, but the Garage
        # starts managing activities only from this point onward.
        if garage_start_epoch is not None:
            clauses.append("COALESCE(a.started_at_epoch, 0) >= ?")
            values.append(garage_start_epoch)
        title_query = str(filters.get("title") or "").strip()
        if title_query:
            clauses.append("LOWER(COALESCE(a.name, '')) LIKE ?")
            values.append(f"%{title_query.lower()}%")
        min_distance = filters.get("min_distance")
        if min_distance is not None:
            clauses.append("COALESCE(a.distance_m, 0) >= ?")
            values.append(float(min_distance) * 1000)
        date_from = str(filters.get("date_from") or "").strip()
        if date_from:
            clauses.append("COALESCE(a.started_at, '') >= ?")
            values.append(f"{date_from}T00:00:00")
        date_to = str(filters.get("date_to") or "").strip()
        if date_to:
            clauses.append("COALESCE(a.started_at, '') <= ?")
            values.append(f"{date_to}T23:59:59.999999Z")
        selected_connections = [str(item) for item in filters.get("connections", [])]
        if selected_connections:
            placeholders = ", ".join("?" for _ in selected_connections)
            provider_links_query = (
                "SELECT COUNT(DISTINCT filter_pc.identifier) FROM activity_provider_links filter_apl "
                "JOIN provider_activities filter_pa ON filter_pa.id=filter_apl.provider_activity_id "
                "JOIN provider_connections filter_pc ON filter_pc.id=filter_pa.connection_id "
                f"WHERE filter_apl.activity_id=a.id AND filter_pc.identifier IN ({placeholders})"
            )
            if filters.get("connection_match") == "all":
                clauses.append(f"({provider_links_query}) = ?")
                values.extend(selected_connections)
                values.append(len(selected_connections))
            else:
                clauses.append(f"({provider_links_query}) > 0")
                values.extend(selected_connections)
        selected_bikes = [str(item) for item in filters.get("bikes", [])]
        if selected_bikes:
            placeholders = ", ".join("?" for _ in selected_bikes)
            clauses.append(
                "EXISTS (SELECT 1 FROM activity_bike_assignments filter_aba "
                "JOIN bikes filter_b ON filter_b.id=filter_aba.bike_id "
                f"WHERE filter_aba.activity_id=a.id AND filter_b.identifier IN ({placeholders}))"
            )
            values.extend(selected_bikes)
        if filters.get("todo"):
            clauses.append(
                "(a.expected_bike_count IS NULL OR "
                "(SELECT COUNT(*) FROM activity_bike_assignments todo_aba WHERE todo_aba.activity_id=a.id) "
                "!= a.expected_bike_count OR "
                "EXISTS ("
                "SELECT 1 FROM activity_provider_links todo_apl "
                "JOIN provider_activities todo_pa ON todo_pa.id=todo_apl.provider_activity_id "
                "JOIN provider_connections todo_pc ON todo_pc.id=todo_pa.connection_id "
                "JOIN activity_bike_assignments todo_aba ON todo_aba.activity_id=a.id "
                "JOIN bike_strava_gear_mappings todo_mapping ON todo_mapping.bike_id=todo_aba.bike_id "
                "JOIN strava_gears todo_sg ON todo_sg.id=todo_mapping.strava_gear_id "
                "WHERE todo_apl.activity_id=a.id AND todo_pc.provider_type='STRAVA_PROXY' "
                "AND todo_sg.provider_connection_id=todo_pc.id "
                "AND todo_aba.slot_index=("
                "SELECT MIN(slot_index) FROM activity_bike_assignments first_aba "
                "JOIN bike_strava_gear_mappings first_mapping ON first_mapping.bike_id=first_aba.bike_id "
                "JOIN strava_gears first_sg ON first_sg.id=first_mapping.strava_gear_id "
                "WHERE first_aba.activity_id=a.id AND first_sg.provider_connection_id=todo_pc.id"
                ") "
                "AND (CASE WHEN json_valid(todo_pa.raw_json) "
                "THEN COALESCE(json_extract(todo_pa.raw_json, '$.gear_id'), '') ELSE '' END) "
                "<> todo_sg.external_gear_id"
                "))"
            )
        clauses.insert(0, "a.deleted_at IS NULL")
        where_clause = f"WHERE {' AND '.join(clauses)}"
        activity_result_summary = dict(db.execute(
            f"""
            SELECT COUNT(*) AS activity_count,
                   COALESCE(SUM(a.distance_m), 0) AS total_distance_m
            FROM activities a
            {where_clause}
            """,
            values,
        ).fetchone())
        activities = [dict(row) for row in db.execute(
            f"""
            SELECT a.*, COUNT(apl.provider_activity_id) AS source_count,
                   GROUP_CONCAT(pc.display_name, ', ') AS sources
            FROM activities a
            LEFT JOIN activity_provider_links apl ON apl.activity_id = a.id
            LEFT JOIN provider_activities pa ON pa.id = apl.provider_activity_id
            LEFT JOIN provider_connections pc ON pc.id = pa.connection_id
            {where_clause}
            GROUP BY a.id
            ORDER BY COALESCE(a.started_at_epoch, 0) DESC, a.id DESC
            LIMIT 100
            """,
            values,
        ).fetchall()]
        # The stream needs a compact, view-only route preview and a useful
        # duration without loading a full provider detail page per card.
        provider_data: dict[int, list[dict[str, object]]] = {}
        for row in db.execute(
            """
            SELECT apl.activity_id, pa.raw_json, pc.provider_type
            FROM activity_provider_links apl
            JOIN provider_activities pa ON pa.id = apl.provider_activity_id
            JOIN provider_connections pc ON pc.id = pa.connection_id
            """
        ).fetchall():
            provider_data.setdefault(row["activity_id"], []).append(dict(row))
        for activity in activities:
            activity["route_polyline"] = None
            activity["duration_label"] = "—"
            # Keep one entry per linked source: two Strava accounts should
            # deliberately appear as two Strava marks in the stream.
            activity["provider_types"] = [
                str(provider["provider_type"])
                for provider in provider_data.get(activity["id"], [])
            ]
            for provider in provider_data.get(activity["id"], []):
                try:
                    payload = json.loads(str(provider.get("raw_json") or "{}"))
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                if activity["route_polyline"] is None:
                    activity["route_polyline"] = (
                        (payload.get("map") or {}).get("summary_polyline")
                        or payload.get("summary_polyline")
                        or payload.get("polyline")
                        or (payload.get("raw_hammerhead") or {}).get("polyline")
                    )
                if activity["duration_label"] == "—":
                    raw_duration = (
                        payload.get("duration")
                        if provider["provider_type"] == "HAMMERHEAD"
                        else payload.get("moving_time") or payload.get("elapsed_time")
                    )
                    activity["duration_label"] = duration_label(
                        raw_duration, milliseconds=provider["provider_type"] == "HAMMERHEAD"
                    )
        activity_bikes: dict[int, list[dict[str, object]]] = {}
        for row in db.execute(
            """
            SELECT aba.activity_id, aba.slot_index, b.id, b.name, b.identifier, b.photo_filename
            FROM activity_bike_assignments aba
            JOIN bikes b ON b.id = aba.bike_id
            ORDER BY aba.activity_id, aba.slot_index
            """
        ).fetchall():
            item = dict(row)
            activity_bikes.setdefault(item["activity_id"], []).append(item)
        for activity in activities:
            assigned_bikes = activity_bikes.get(activity["id"], [])
            activity["bike_assignments"] = assigned_bikes
            activity["assigned_bike_count"] = len(assigned_bikes)
            expected_count = activity.get("expected_bike_count")
            activity["strava_gear_mismatches"] = strava_gear_mismatches(db, activity["id"])
            activity["strava_gear_todo"] = bool(activity["strava_gear_mismatches"])
            activity["bike_assignment_todo"] = (
                not expected_count
                or len(assigned_bikes) != expected_count
                or activity["strava_gear_todo"]
            )
        attention_activities = [activity for activity in activities if activity["bike_assignment_todo"]]
        bikes = [dict(row) for row in db.execute("SELECT * FROM bikes ORDER BY name COLLATE NOCASE").fetchall()]
        mileage_settings = attach_bike_mileage(db, bikes, user["id"])
        for bike in bikes:
            latest_activity = db.execute(
                """
                SELECT a.id, a.name, a.started_at, a.started_at_epoch, a.distance_m
                FROM activity_bike_assignments aba
                JOIN activities a ON a.id = aba.activity_id
                WHERE aba.bike_id = ? AND a.deleted_at IS NULL
                  AND (? IS NULL OR COALESCE(a.started_at_epoch, 0) >= ?)
                ORDER BY COALESCE(a.started_at_epoch, 0) DESC, a.id DESC
                LIMIT 1
                """,
                (bike["id"], garage_start_epoch, garage_start_epoch),
            ).fetchone()
            bike["latest_activity"] = dict(latest_activity) if latest_activity else None
        historical_activity_count = 0
        if garage_start_epoch is not None:
            historical_activity_count = db.execute(
                "SELECT COUNT(*) FROM activities WHERE deleted_at IS NULL "
                "AND COALESCE(started_at_epoch, 0) < ?",
                (garage_start_epoch,),
            ).fetchone()[0]
        rules = db.execute("SELECT * FROM resolver_rules WHERE user_id = ? ORDER BY priority, id", (user["id"],)).fetchall()
    test_connection_id = request.query_params.get("test_connection")
    tested_connection = next(
        (item for item in connections if str(item["id"]) == test_connection_id), None
    )
    return {
        "request": request,
        "user": user,
        "connections": connections,
        "activities": activities,
        "historical_activity_count": historical_activity_count,
        "attention_activities": attention_activities,
        "activity_filters": filters,
        "activity_result_summary": activity_result_summary,
        "activity_filter_connections": [
            {"identifier": item["identifier"], "display_name": item["display_name"], "provider_type": item["provider_type"]}
            for item in connections
        ],
        "activity_filter_bikes": [
            {"identifier": bike["identifier"], "name": bike["name"], "photo_filename": bike["photo_filename"]}
            for bike in bikes
        ],
        "bikes": bikes,
        "mileage_settings": mileage_settings,
        "rules": rules,
        "providers": PROVIDERS,
        "tested_connection": tested_connection,
        **extra,
    }


def reapply_rules_to_all_activities(db: object) -> tuple[int, int]:
    """Explicitly run enabled rules against activities managed by the Garage."""
    setting = db.execute(
        "SELECT mileage_tracking_started_at_epoch FROM user_settings ORDER BY user_id LIMIT 1"
    ).fetchone()
    garage_start_epoch = setting["mileage_tracking_started_at_epoch"] if setting else None
    if garage_start_epoch is None:
        activities = db.execute(
            "SELECT id, sport_type FROM activities WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
    else:
        activities = db.execute(
            "SELECT id, sport_type FROM activities WHERE deleted_at IS NULL "
            "AND COALESCE(started_at_epoch, 0) >= ? ORDER BY id",
            (garage_start_epoch,),
        ).fetchall()
    failures = 0
    for activity in activities:
        try:
            apply_rule(db, activity["id"], activity["sport_type"], None)
        except Exception:
            failures += 1
    return len(activities), failures


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", page_context(request))


@app.get("/providers")
def providers(request: Request):
    return templates.TemplateResponse(request, "providers.html", page_context(request))


@app.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", page_context(request))


@app.post("/settings/mileage")
async def update_mileage_settings(request: Request):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    raw_started_at = form.get("garage_started_at", form.get("mileage_tracking_started_at", [""]))[0].strip()
    started_at: str | None = None
    started_epoch: int | None = None
    if raw_started_at:
        try:
            local_time = datetime.fromisoformat(raw_started_at)
            if local_time.tzinfo is not None:
                local_time = local_time.replace(tzinfo=None)
            started_at = local_time.isoformat(timespec="minutes")
            started_epoch = int(local_time.replace(tzinfo=LOCAL_TIMEZONE).timestamp())
        except ValueError:
            return RedirectResponse("/settings?notice=Choose+a+valid+Garage+start", status_code=303)
    user = initial_user()
    with connection() as db:
        db.execute(
            """INSERT INTO user_settings (user_id,mileage_tracking_started_at,mileage_tracking_started_at_epoch,updated_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET mileage_tracking_started_at=excluded.mileage_tracking_started_at,
                 mileage_tracking_started_at_epoch=excluded.mileage_tracking_started_at_epoch, updated_at=CURRENT_TIMESTAMP""",
            (user["id"], started_at, started_epoch),
        )
    notice = "Garage start updated" if started_at else "Garage start is not set"
    return RedirectResponse("/settings?" + urlencode({"notice": notice}), status_code=303)


@app.get("/activities")
def activities_page(request: Request):
    raw_min_distance = request.query_params.get("min_distance", "").strip()
    try:
        min_distance = max(0.0, float(raw_min_distance)) if raw_min_distance else None
    except ValueError:
        min_distance = None
    filters: dict[str, object] = {
        "todo": request.query_params.get("todo") == "1",
        "connections": request.query_params.getlist("connection"),
        "bikes": request.query_params.getlist("bike"),
        "connection_match": "all" if request.query_params.get("connection_match") == "all" else "any",
        "min_distance": min_distance,
        "date_from": request.query_params.get("date_from", "").strip(),
        "date_to": request.query_params.get("date_to", "").strip(),
        "title": request.query_params.get("title", "").strip(),
    }
    filters["active_count"] = sum(
        (1 if filters["todo"] else 0,
         len(filters["connections"]),
         len(filters["bikes"]),
         1 if filters["min_distance"] is not None else 0,
         1 if filters["date_from"] else 0,
         1 if filters["date_to"] else 0,
         1 if filters["title"] else 0)
    )
    return templates.TemplateResponse(request, "activities.html", page_context(request, activity_filters=filters))


@app.get("/rules")
def rules_index(request: Request):
    with connection() as db:
        all_activity_count = db.execute("SELECT COUNT(*) FROM activities WHERE deleted_at IS NULL").fetchone()[0]
        garage_start = db.execute(
            "SELECT mileage_tracking_started_at_epoch FROM user_settings WHERE user_id=?",
            (initial_user()["id"],),
        ).fetchone()
        garage_start_epoch = garage_start["mileage_tracking_started_at_epoch"] if garage_start else None
        eligible_activity_count = (
            db.execute(
                "SELECT COUNT(*) FROM activities WHERE deleted_at IS NULL AND started_at_epoch>=?",
                (garage_start_epoch,),
            ).fetchone()[0]
            if garage_start_epoch is not None
            else all_activity_count
        )
        enabled_rule_count = db.execute("SELECT COUNT(*) FROM resolver_rules WHERE enabled=1").fetchone()[0]
    return templates.TemplateResponse(
        request,
        "rules.html",
        page_context(
            request,
            rule_execute_activity_count=eligible_activity_count,
            rule_execute_historical_activity_count=all_activity_count - eligible_activity_count,
            rule_execute_has_garage_start=garage_start_epoch is not None,
            enabled_rule_count=enabled_rule_count,
        ),
    )


@app.get("/rules")
def rules_page(request: Request):
    return rules_index(request)


@app.post("/rules/execute-all")
def execute_rules_on_all_activities():
    """Run rules only on explicit user request, never as a side effect of saving."""
    with connection() as db:
        enabled_rule_count = db.execute("SELECT COUNT(*) FROM resolver_rules WHERE enabled=1").fetchone()[0]
        if not enabled_rule_count:
            return RedirectResponse("/rules?notice=No+enabled+rules+to+execute", status_code=303)
        processed, failures = reapply_rules_to_all_activities(db)
    notice = f"Executed {enabled_rule_count} enabled rule{'s' if enabled_rule_count != 1 else ''} for {processed} activit{'ies' if processed != 1 else 'y'}"
    if failures:
        notice += f" ({failures} failed)"
    return RedirectResponse("/rules?" + urlencode({"notice": notice}), status_code=303)


@app.get("/rules/new")
def new_rule(request: Request):
    return templates.TemplateResponse(request, "rule_count_form.html", page_context(request, rule=None))


@app.post("/rules")
async def create_rule(request: Request):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    name = form.get("name", [""])[0].strip(); priority = form.get("priority", ["100"])[0]
    bike_count = form.get("bike_count", ["1"])[0]
    is_catch_all = 1 if form.get("is_catch_all", [""])[0] else 0
    expression = form.get("expression", ["linked_provider_activities"])[0]
    condition_operator = form.get("condition_operator", [">="])[0]
    if condition_operator not in (">", ">=", "=", "<", "<="):
        condition_operator = ">="
    try:
        condition_value = int(form.get("condition_value", ["1"])[0])
    except ValueError:
        condition_value = 1
    try:
        priority_value, count_value = int(priority), max(1, int(bike_count))
    except ValueError:
        priority_value, count_value = 100, 1
    if not name:
        return RedirectResponse("/rules/new?notice=Rule+name+is+required", status_code=303)
    if not validate_expression(expression):
        return RedirectResponse("/rules/new?" + urlencode({"notice": expression_error(expression)}), status_code=303)
    user = initial_user()
    with connection() as db:
        cursor = db.execute("INSERT INTO resolver_rules (user_id,name,priority,sport_type,provider_account_id,bike_count,expression,is_catch_all,condition_operator,condition_value) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (user["id"], name, priority_value, None, None, count_value, expression, 0, condition_operator, condition_value))
        rule_id = cursor.lastrowid
    return RedirectResponse(f"/rules/{rule_id}/edit?notice=Rule+created", status_code=303)

@app.get("/rules/{rule_id}/edit")
def edit_rule(request: Request, rule_id: int):
    with connection() as db:
        rule = db.execute("SELECT * FROM resolver_rules WHERE id = ?", (rule_id,)).fetchone()
    if rule is None:
        return RedirectResponse("/rules?notice=Rule+not+found", status_code=303)
    return templates.TemplateResponse(request, "rule_count_form.html", page_context(request, rule=rule, editing=True))

@app.post("/rules/{rule_id}/edit")
async def update_rule(request: Request, rule_id: int):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    name = form.get("name", [""])[0].strip(); expression = form.get("expression", ["activity.providerActivities.count"])[0]
    try:
        priority, fixed_count, condition_value = (int(form.get(k, [d])[0]) for k, d in (("priority", "100"), ("bike_count", "1"), ("condition_value", "1")))
    except ValueError:
        priority, fixed_count, condition_value = 100, 1, 1
    operator = form.get("condition_operator", [">="])[0]
    with connection() as db:
        rule = db.execute("SELECT * FROM resolver_rules WHERE id = ?", (rule_id,)).fetchone()
    if rule is None:
        return RedirectResponse("/rules?notice=Rule+not+found", status_code=303)
    if not name or operator not in (">", ">=", "=", "<", "<=") or not validate_expression(expression):
        # Keep the submitted source intact on validation errors. Redirecting
        # here used to reload the persisted rule and silently discard the
        # work the user needed to inspect or copy.
        error = "Rule name is required." if not name else expression_error(expression)
        rule_view = dict(rule)
        rule_view.update({"name": name, "expression": expression, "priority": priority, "bike_count": fixed_count})
        return templates.TemplateResponse(
            request,
            "rule_count_form.html",
            page_context(request, rule=rule_view, editing=True, form_error=error),
            status_code=422,
        )
    with connection() as db:
        db.execute("UPDATE resolver_rules SET name=?,priority=?,bike_count=?,expression=?,condition_operator=?,condition_value=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (name, priority, fixed_count, expression, operator, condition_value, rule_id))
    return RedirectResponse(f"/rules/{rule_id}/edit?notice=Rule+updated", status_code=303)

@app.post("/rules/{rule_id}/test-inline")
async def test_rule_inline(request: Request, rule_id: int):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    activity_id = int(form.get("activity_id", ["0"])[0] or 0)
    execution_mode = form.get("mode", ["preview"])[0]
    if execution_mode not in {"preview", "apply"}:
        execution_mode = "preview"
    with connection() as db:
        rule = db.execute("SELECT * FROM resolver_rules WHERE id=?", (rule_id,)).fetchone()
        activity = db.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        bike_rows = [dict(row) for row in db.execute("SELECT * FROM bikes ORDER BY name").fetchall()]
        for bike in bike_rows:
            bike["components"] = [dict(component) for component in db.execute(
                "SELECT component_type,ant_device_number FROM bike_components WHERE bike_id=?", (bike["id"],)
            ).fetchall()]
        assigned_rows = db.execute("SELECT b.* FROM activity_bike_assignments aba JOIN bikes b ON b.id=aba.bike_id WHERE aba.activity_id=? ORDER BY aba.slot_index", (activity_id,)).fetchall()
        rows = db.execute("SELECT pa.id AS provider_activity_id, pc.provider_type AS provider, pc.identifier AS connection, pc.external_account_id AS account, pa.name AS title, pa.sport_type AS sport_type, pa.distance_m AS distance_m, pa.started_at AS started_at, pa.raw_json AS raw_json FROM activity_provider_links apl JOIN provider_activities pa ON pa.id=apl.provider_activity_id JOIN provider_connections pc ON pc.id=pa.connection_id WHERE apl.activity_id=?", (activity_id,)).fetchall()
        rows = attach_hardware_observations(db, rows)
    if rule is None: return RedirectResponse("/rules?notice=Rule+not+found", status_code=303)
    # Save the current edit form before running the test, so the test always
    # evaluates exactly what the user sees in the editor.
    submitted_name = form.get("name", [rule["name"]])[0].strip()
    submitted_expression = form.get("expression", [rule["expression"]])[0]
    if not submitted_name or not validate_expression(submitted_expression):
        error = "Rule name is required." if not submitted_name else expression_error(submitted_expression)
        # A Save and Run Test action should keep the user in the editor and
        # surface DSL diagnostics beside the selected activity, not lose the
        # work through a redirect-level notification.
        rule_view = dict(rule)
        rule_view["name"] = submitted_name or rule["name"]
        rule_view["expression"] = submitted_expression
        return templates.TemplateResponse(
            request,
            "rule_count_form.html",
            page_context(
                request, rule=rule_view, editing=True, test_result="Invalid rule",
                test_logs=[error], selected_activity=activity_id, form_error=error,
            ),
        )
    with connection() as db:
        db.execute("UPDATE resolver_rules SET name=?, expression=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (submitted_name, submitted_expression, rule_id))
        rule = db.execute("SELECT * FROM resolver_rules WHERE id=?", (rule_id,)).fetchone()
        # A manual test is intentionally allowed for a disabled rule. Disabled
        # rules remain excluded from bulk execution; an explicit "Run" is the
        # only editor action that is allowed to change the chosen activity.
        activity = db.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        bike_rows = [dict(row) for row in db.execute("SELECT * FROM bikes ORDER BY name").fetchall()]
        for bike in bike_rows:
            bike["components"] = [dict(component) for component in db.execute(
                "SELECT component_type,ant_device_number FROM bike_components WHERE bike_id=?", (bike["id"],)
            ).fetchall()]
        assigned_rows = db.execute("SELECT b.* FROM activity_bike_assignments aba JOIN bikes b ON b.id=aba.bike_id WHERE aba.activity_id=? ORDER BY aba.slot_index", (activity_id,)).fetchall()
        rows = db.execute("SELECT pa.id AS provider_activity_id, pc.provider_type AS provider, pc.identifier AS connection, pc.external_account_id AS account, pa.name AS title, pa.sport_type AS sport_type, pa.distance_m AS distance_m, pa.started_at AS started_at, pa.raw_json AS raw_json FROM activity_provider_links apl JOIN provider_activities pa ON pa.id=apl.provider_activity_id JOIN provider_connections pc ON pc.id=pa.connection_id WHERE apl.activity_id=?", (activity_id,)).fetchall()
        rows = attach_hardware_observations(db, rows)
        logs: list[str] = []
        context = ActivityContext(rows, dict(activity) if activity else None, bike_rows, [dict(row) for row in assigned_rows])
        result = evaluate_expression(rule["expression"], context, logs)
        resolved_count = max(1, int(context.bikeCount or activity["expected_bike_count"] or 1))
        resolved_title = activity["manual_title"] or context.title or activity["name"]
        result_text = "null" if result is None else str(result).lower() if isinstance(result, bool) else str(result)
        logs.append(f"Resolved title: {resolved_title}")
        logs.append(f"Resolved bikeCount: {resolved_count}")
        if context.bikes_changed:
            logs.append(
                "Resolved bikes: " + ", ".join(bike.identifier for bike in context.bikes if bike)
                if context.bikes else "Resolved bikes: none (RULE assignments cleared)"
            )
        elif context.bikes:
            logs.append("Resolved bikes: " + ", ".join(bike.identifier for bike in context.bikes if bike))
        if execution_mode == "apply":
            db.execute(
                "UPDATE activities SET name=?, expected_bike_count=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (resolved_title, resolved_count, activity_id),
            )
            if context.bikes_changed:
                db.execute("DELETE FROM activity_bike_assignments WHERE activity_id=? AND source='RULE'", (activity_id,))
                for slot_index, bike in enumerate(context.bikes[:resolved_count], start=1):
                    if bike and bike.id:
                        db.execute(
                            "INSERT OR REPLACE INTO activity_bike_assignments (activity_id,bike_id,slot_index,source) VALUES (?,?,?,'RULE')",
                            (activity_id, bike.id, slot_index),
                        )
            # Resolver results remain local. Strava is deliberately changed
            # only through the explicit confirmation on an activity detail.
            db.execute(
                """INSERT INTO activity_rule_runs
                   (activity_id,resolution_id,rule_id,rule_name,result_text,log_output_json,applied)
                   VALUES (?,?,?,?,?,?,?)""",
                (activity_id, f"manual-{secrets.token_hex(8)}", rule["id"], rule["name"], result_text, json.dumps(logs), 1),
            )
    return templates.TemplateResponse(
        request,
        "rule_count_form.html",
        page_context(
            request,
            rule=rule,
            editing=True,
            test_result=result,
            test_logs=logs,
            test_applied=execution_mode == "apply",
            selected_activity=activity_id,
            selected_activity_public_id=activity["public_id"],
        ),
    )


@app.post("/rules/{rule_id}/delete")
def delete_rule(rule_id: int):
    with connection() as db:
        db.execute("DELETE FROM resolver_rules WHERE id = ?", (rule_id,))
    return RedirectResponse("/rules?notice=Rule+deleted", status_code=303)


@app.post("/rules/{rule_id}/logs/delete")
def delete_rule_logs(rule_id: int):
    """Clear this rule's persisted run history without changing the rule."""
    with connection() as db:
        rule = db.execute("SELECT name FROM resolver_rules WHERE id=?", (rule_id,)).fetchone()
        if rule is None:
            return RedirectResponse("/rules?notice=Rule+not+found", status_code=303)
        deleted = db.execute("DELETE FROM activity_rule_runs WHERE rule_id=?", (rule_id,)).rowcount
    label = "log entry" if deleted == 1 else "log entries"
    return RedirectResponse(
        "/rules?" + urlencode({"notice": f"Deleted {deleted} {label} for {rule['name']}"}),
        status_code=303,
    )

@app.get("/rules/{rule_id}/test")
def test_rule_page(request: Request, rule_id: int):
    with connection() as db: rule = db.execute("SELECT * FROM resolver_rules WHERE id=?", (rule_id,)).fetchone()
    if rule is None: return RedirectResponse("/rules?notice=Rule+not+found", status_code=303)
    return templates.TemplateResponse(request, "rule_test.html", page_context(request, rule=rule))

@app.post("/rules/{rule_id}/test")
async def test_rule(request: Request, rule_id: int):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    activity_id = int(form.get("activity_id", ["0"])[0] or 0)
    with connection() as db:
        rule = db.execute("SELECT * FROM resolver_rules WHERE id=?", (rule_id,)).fetchone()
        rows = db.execute("""SELECT pc.provider_type AS provider FROM activity_provider_links apl
            JOIN provider_activities pa ON pa.id=apl.provider_activity_id JOIN provider_connections pc ON pc.id=pa.connection_id
            WHERE apl.activity_id=?""", (activity_id,)).fetchall()
    if rule is None: return RedirectResponse("/rules?notice=Rule+not+found", status_code=303)
    try:
        result = evaluate_expression(rule["expression"], ActivityContext([dict(row) for row in rows]))
        result_label = "null (next rule)" if result is None else str(result).lower() if isinstance(result, bool) else str(result)
    except Exception as error:
        result_label = f"Invalid expression: {error}"
    return templates.TemplateResponse(request, "rule_test.html", page_context(request, rule=rule, result=result_label, selected_activity=activity_id))

@app.post("/rules/reorder")
async def reorder_rules(request: Request):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    try:
        ids = [int(value) for value in form.get("order", [""])[0].split(",") if value]
    except ValueError:
        ids = []
    with connection() as db:
        for priority, rule_id in enumerate(ids, start=1):
            db.execute("UPDATE resolver_rules SET priority=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (priority, rule_id))
    return RedirectResponse("/rules?notice=Rule+order+updated", status_code=303)


@app.post("/rules/{rule_id}/enabled")
async def set_rule_enabled(request: Request, rule_id: int):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    enabled = 1 if form.get("enabled", ["0"])[0] == "1" else 0
    with connection() as db:
        rule = db.execute("SELECT id FROM resolver_rules WHERE id=?", (rule_id,)).fetchone()
        if rule is None:
            return RedirectResponse("/rules?notice=Rule+not+found", status_code=303)
        db.execute(
            "UPDATE resolver_rules SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (enabled, rule_id),
        )
    return RedirectResponse("/rules?" + urlencode({"notice": "Rule enabled" if enabled else "Rule disabled"}), status_code=303)


@app.get("/activities/{activity_uuid}")
def activity_detail(request: Request, activity_uuid: str):
    with connection() as db:
        activity = db.execute("SELECT * FROM activities WHERE public_id = ? AND deleted_at IS NULL", (activity_uuid,)).fetchone()
        if activity is None:
            return RedirectResponse("/activities?notice=Activity+not+found", status_code=303)
        activity_id = activity["id"]
        provider_rows = db.execute(
            """
            SELECT pa.*, pc.display_name AS connection_name, pc.provider_type, pc.endpoint_url, pc.access_token
            FROM provider_activities pa
            JOIN activity_provider_links apl ON apl.provider_activity_id = pa.id
            JOIN provider_connections pc ON pc.id = pa.connection_id
            WHERE apl.activity_id = ? ORDER BY pa.started_at_epoch ASC, pa.id ASC
            """,
            (activity_id,),
        ).fetchall()
        assignments = db.execute(
            "SELECT aba.slot_index, b.* FROM activity_bike_assignments aba JOIN bikes b ON b.id=aba.bike_id WHERE aba.activity_id=? ORDER BY aba.slot_index",
            (activity_id,),
        ).fetchall()
        run_rows = db.execute(
            """SELECT resolution_id, rule_name, result_text, log_output_json, applied, created_at, id
               FROM activity_rule_runs WHERE activity_id=? ORDER BY id DESC LIMIT 180""",
            (activity_id,),
        ).fetchall()
        ui_log_rows = db.execute(
            "SELECT logger,message,created_at,id FROM activity_log_entries WHERE activity_id=? ORDER BY id DESC LIMIT 180",
            (activity_id,),
        ).fetchall()
        strava_mismatches = {
            item["provider_activity_id"]: item
            for item in strava_gear_mismatches(db, activity_id)
        }
    providers = []
    resolver_runs_by_id: dict[str, dict[str, object]] = {}
    for row in run_rows:
        item = dict(row)
        try:
            item["logs"] = json.loads(item["log_output_json"] or "[]")
        except json.JSONDecodeError:
            item["logs"] = []
        group = resolver_runs_by_id.setdefault(
            item["resolution_id"], {"created_at": item["created_at"], "rules": []}
        )
        group["rules"].insert(0, item)
    resolver_runs = list(resolver_runs_by_id.values())
    activity_logs: list[dict[str, object]] = []
    for row in run_rows:
        item = dict(row)
        try:
            messages = json.loads(item["log_output_json"] or "[]")
        except json.JSONDecodeError:
            messages = []
        # One resolver execution is one log event. Its individual print calls
        # remain readable as separate lines inside that event instead of
        # flooding the table with one row per print statement.
        if not messages:
            messages = [f"Result: {item['result_text']}" + (" · applied" if item["applied"] else "")]
        activity_logs.append({
            "created_at": item["created_at"],
            "logger": f"rule_{item['rule_name']}",
            "message": "\n".join(str(message) for message in messages),
            "sort_id": item["id"],
        })
    for row in ui_log_rows:
        item = dict(row)
        activity_logs.append({
            "created_at": item["created_at"],
            "logger": item["logger"],
            "message": item["message"],
            "sort_id": item["id"],
        })
    # These timestamps originate from the persisted imports rather than a
    # transient UI event, so they make it possible to reconstruct why a rule
    # ran with only part of an activity's provider context.
    activity_logs.append({
        "created_at": activity["created_at"],
        "logger": "IMPORT",
        "message": "Canonical activity created.",
        "sort_id": -1,
    })
    for source in provider_rows:
        activity_logs.append({
            "created_at": source["imported_at"],
            "logger": "IMPORT",
            "message": f"{source['connection_name']} activity imported.",
            "sort_id": -int(source["id"]),
        })
    activity_logs.sort(key=lambda item: (str(item["created_at"]), int(item["sort_id"])), reverse=True)
    route_polyline = None
    for row in provider_rows:
        item = dict(row)
        item["strava_gear_mismatch"] = strava_mismatches.get(item["id"])
        try:
            payload = json.loads(item["raw_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        item["device_name"] = payload.get("device_name") or ""
        item["gear_id"] = payload.get("gear_id") or ""
        item["moving_time"] = payload.get("moving_time")
        raw_duration = payload.get("duration") if item["provider_type"] == "HAMMERHEAD" else payload.get("moving_time") or payload.get("elapsed_time")
        item["duration_label"] = duration_label(raw_duration, milliseconds=item["provider_type"] == "HAMMERHEAD")
        if route_polyline is None:
            route_polyline = (
                (payload.get("map") or {}).get("summary_polyline")
                or payload.get("polyline")
                or (payload.get("raw_hammerhead") or {}).get("polyline")
            )
        # Older Hammerhead records were imported from summaries before we
        # retained polylines. Enrich exactly this opened activity on demand,
        # rather than slowing every future Sync now run down.
        if route_polyline is None and item["provider_type"] == "HAMMERHEAD":
            try:
                enriched = fetch_hammerhead_activity_detail(
                    item["endpoint_url"], item["access_token"], item["external_activity_id"]
                )
                payload = enriched
                route_polyline = (payload.get("map") or {}).get("summary_polyline")
                with connection() as db:
                    db.execute("UPDATE provider_activities SET raw_json=?, imported_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(enriched), item["id"]))
            except Exception:
                pass
        if item["provider_type"] == "HAMMERHEAD":
            # FIT is fetched lazily for the opened provider activity so normal
            # periodic metadata sync stays quick.  Once decoded, observations
            # are persisted and become available to resolver rules.
            with connection() as db:
                has_hardware = db.execute(
                    "SELECT 1 FROM provider_activity_hardware WHERE provider_activity_id=? LIMIT 1", (item["id"],)
                ).fetchone()
                if has_hardware is None:
                    try:
                        fit_bytes = fetch_hammerhead_activity_fit(item["endpoint_url"], item["access_token"], item["external_activity_id"])
                        observations = store_fit_hardware(db, item["id"], fit_bytes)
                        if observations:
                            apply_rule(db, activity_id, activity["sport_type"], None)
                    except Exception:
                        pass
                item["hardware"] = [dict(hardware) for hardware in db.execute(
                    "SELECT * FROM provider_activity_hardware WHERE provider_activity_id=? ORDER BY id", (item["id"],)
                ).fetchall()]
        providers.append(item)
    return templates.TemplateResponse(
        request,
        "activity_detail.html",
        page_context(request, activity=activity, provider_activities=providers, route_polyline=route_polyline, bike_assignments=assignments, resolver_runs=resolver_runs, activity_logs=activity_logs),
    )


@app.post("/activities/{activity_uuid}/strava-bikes/{provider_activity_id}/update")
def update_activity_strava_bike(activity_uuid: str, provider_activity_id: int):
    """Push one resolved bike to the matching Strava source after confirmation."""
    with connection() as db:
        activity = db.execute(
            "SELECT id FROM activities WHERE public_id=? AND deleted_at IS NULL", (activity_uuid,)
        ).fetchone()
        if activity is None:
            return RedirectResponse("/activities?notice=Activity+not+found", status_code=303)
        source = db.execute(
            """SELECT 1 FROM activity_provider_links apl
               JOIN provider_activities pa ON pa.id=apl.provider_activity_id
               JOIN provider_connections pc ON pc.id=pa.connection_id
               WHERE apl.activity_id=? AND pa.id=? AND pc.provider_type='STRAVA_PROXY'""",
            (activity["id"], provider_activity_id),
        ).fetchone()
        if source is None:
            return RedirectResponse(
                f"/activities/{activity_uuid}?notice=Strava+activity+not+found", status_code=303
            )
        updated, skipped = sync_strava_activity_gears(db, activity["id"], provider_activity_id)
    if updated:
        notice = "Strava bike updated"
    elif skipped:
        notice = "Strava bike was not updated: activity is before Garage start"
    else:
        notice = "Strava bike is already up to date or no matching Bike Garage gear is configured"
    return RedirectResponse(f"/activities/{activity_uuid}?" + urlencode({"notice": notice}), status_code=303)


@app.get("/provider-activities/{provider_activity_id}")
def provider_activity_detail(request: Request, provider_activity_id: int):
    """Show the original provider record behind a canonical activity source."""
    with connection() as db:
        row = db.execute(
            """
            SELECT pa.*, pc.display_name AS connection_name, pc.identifier AS connection_identifier,
                   pc.provider_type, pc.endpoint_url, pc.access_token,
                   a.id AS canonical_activity_id, a.public_id AS canonical_activity_public_id,
                   a.name AS canonical_activity_name,
                   a.sport_type AS canonical_sport_type,
                   a.deleted_at AS canonical_deleted_at
            FROM provider_activities pa
            JOIN provider_connections pc ON pc.id = pa.connection_id
            LEFT JOIN activity_provider_links apl ON apl.provider_activity_id = pa.id
            LEFT JOIN activities a ON a.id = apl.activity_id
            WHERE pa.id = ?
            """,
            (provider_activity_id,),
        ).fetchone()
    if row is None or row["canonical_deleted_at"] is not None:
        return RedirectResponse("/activities?notice=Provider+activity+not+found", status_code=303)

    source = dict(row)
    try:
        payload = json.loads(source.get("raw_json") or "{}")
    except json.JSONDecodeError:
        payload = {}
    source["payload"] = payload
    source["device_name"] = payload.get("device_name") or "—"
    source["gear_id"] = payload.get("gear_id") or ""
    source["route_polyline"] = (
        (payload.get("map") or {}).get("summary_polyline")
        or payload.get("summary_polyline")
        or payload.get("polyline")
        or (payload.get("raw_hammerhead") or {}).get("polyline")
    )
    raw_duration = payload.get("duration") if source["provider_type"] == "HAMMERHEAD" else payload.get("moving_time") or payload.get("elapsed_time")
    source["duration_label"] = duration_label(raw_duration, milliseconds=source["provider_type"] == "HAMMERHEAD")
    source["raw_payload_pretty"] = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    # FIT is loaded only when this Hammerhead source is opened, keeping normal
    # sync quick while making sensor identities available to the resolver.
    with connection() as db:
        if source["provider_type"] == "STRAVA_PROXY":
            # Gear IDs belong to an individual Strava connection/account. Join
            # through that connection so identical-looking gear IDs on another
            # account can never be reported as the wrong garage bike.
            gear = db.execute(
                """SELECT sg.external_gear_id, sg.name, sg.distance_m,
                          b.id AS bike_id, b.name AS bike_name, b.identifier AS bike_identifier
                   FROM strava_gears sg
                   LEFT JOIN bike_strava_gear_mappings mapping ON mapping.strava_gear_id = sg.id
                   LEFT JOIN bikes b ON b.id = mapping.bike_id
                   WHERE sg.provider_connection_id=? AND sg.external_gear_id=?""",
                (source["connection_id"], source["gear_id"]),
            ).fetchone() if source["gear_id"] else None
            source["strava_gear"] = dict(gear) if gear else None
        if source["provider_type"] == "HAMMERHEAD":
            hardware_exists = db.execute(
                "SELECT 1 FROM provider_activity_hardware WHERE provider_activity_id=? LIMIT 1",
                (provider_activity_id,),
            ).fetchone()
            if hardware_exists is None:
                try:
                    fit_bytes = fetch_hammerhead_activity_fit(
                        source["endpoint_url"], source["access_token"], source["external_activity_id"]
                    )
                    observations = store_fit_hardware(db, provider_activity_id, fit_bytes)
                    if observations and source.get("canonical_activity_id"):
                        apply_rule(db, source["canonical_activity_id"], source.get("canonical_sport_type"), None)
                except Exception:
                    pass
        source["hardware"] = [dict(item) for item in db.execute(
            "SELECT * FROM provider_activity_hardware WHERE provider_activity_id=? ORDER BY component_type, ant_device_number, id",
            (provider_activity_id,),
        ).fetchall()]

    return templates.TemplateResponse(request, "provider_activity_detail.html", page_context(request, source=source))


@app.post("/activities/{activity_uuid}/rules/{rule_id}/test")
def test_rule_from_activity(activity_uuid: str, rule_id: int):
    """Explicitly test one saved rule against this activity and retain its log."""
    with connection() as db:
        activity = db.execute(
            "SELECT * FROM activities WHERE public_id=? AND deleted_at IS NULL", (activity_uuid,)
        ).fetchone()
        rule = db.execute("SELECT * FROM resolver_rules WHERE id=?", (rule_id,)).fetchone()
        if activity is None or rule is None:
            return RedirectResponse(
                f"/activities/{activity_uuid}?notice=Activity+or+rule+not+found", status_code=303
            )
        activity_id = activity["id"]
        bike_rows = [dict(row) for row in db.execute("SELECT * FROM bikes ORDER BY name").fetchall()]
        for bike in bike_rows:
            bike["components"] = [dict(component) for component in db.execute(
                "SELECT component_type,ant_device_number FROM bike_components WHERE bike_id=?", (bike["id"],)
            ).fetchall()]
        assigned_rows = db.execute(
            "SELECT b.* FROM activity_bike_assignments aba JOIN bikes b ON b.id=aba.bike_id "
            "WHERE aba.activity_id=? ORDER BY aba.slot_index", (activity_id,)
        ).fetchall()
        provider_rows = db.execute(
            """SELECT pa.id AS provider_activity_id, pc.provider_type AS provider,
                      pc.identifier AS connection, pc.external_account_id AS account,
                      pa.name AS title, pa.sport_type, pa.distance_m, pa.started_at, pa.raw_json
               FROM activity_provider_links apl
               JOIN provider_activities pa ON pa.id=apl.provider_activity_id
               JOIN provider_connections pc ON pc.id=pa.connection_id
               WHERE apl.activity_id=?""",
            (activity_id,),
        ).fetchall()
        provider_rows = attach_hardware_observations(db, provider_rows)
        logs: list[str] = []
        applied = False
        try:
            context = ActivityContext(provider_rows, dict(activity), bike_rows, [dict(row) for row in assigned_rows])
            result = evaluate_expression(rule["expression"], context, logs)
            resolved_count = max(1, int(context.bikeCount or activity["expected_bike_count"] or 1))
            resolved_title = activity["manual_title"] or context.title or activity["name"]
            db.execute(
                "UPDATE activities SET name=?, expected_bike_count=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (resolved_title, resolved_count, activity_id),
            )
            if context.bikes_changed:
                db.execute("DELETE FROM activity_bike_assignments WHERE activity_id=? AND source='RULE'", (activity_id,))
                for slot_index, bike in enumerate(context.bikes[:resolved_count], start=1):
                    if bike and bike.id:
                        db.execute(
                            "INSERT OR REPLACE INTO activity_bike_assignments "
                            "(activity_id,bike_id,slot_index,source) VALUES (?,?,?,'RULE')",
                            (activity_id, bike.id, slot_index),
                        )
            result_text = "null" if result is None else str(result).lower() if isinstance(result, bool) else str(result)
            applied = result is not None and result is not False
            logs.append(f"Resolved title: {resolved_title}")
            logs.append(f"Resolved bikeCount: {resolved_count}")
            if context.bikes_changed:
                logs.append(
                    "Resolved bikes: " + ", ".join(bike.identifier for bike in context.bikes if bike)
                    if context.bikes else "Resolved bikes: none (RULE assignments cleared)"
                )
            elif context.bikes:
                logs.append("Resolved bikes: " + ", ".join(bike.identifier for bike in context.bikes if bike))
        except Exception as error:
            result_text = f"error: {error}"
            logs.append(f"Rule test failed: {error}")
        db.execute(
            """INSERT INTO activity_rule_runs
               (activity_id,resolution_id,rule_id,rule_name,result_text,log_output_json,applied)
               VALUES (?,?,?,?,?,?,?)""",
            (activity_id, f"manual-{secrets.token_hex(8)}", rule["id"], rule["name"], result_text, json.dumps(logs), int(applied)),
        )
    notice = f"Rule {rule['name']} tested" if applied else f"Rule {rule['name']} did not apply"
    return RedirectResponse(f"/activities/{activity_uuid}?" + urlencode({"notice": notice}), status_code=303)


@app.post("/activities/{activity_uuid}/title")
async def update_activity_title(activity_uuid: str, request: Request):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    with connection() as db:
        activity = db.execute("SELECT * FROM activities WHERE public_id = ? AND deleted_at IS NULL", (activity_uuid,)).fetchone()
        if activity is None:
            return RedirectResponse("/activities?notice=Activity+not+found", status_code=303)
        activity_id = activity["id"]
        previous_title = activity["name"]
        try:
            requested_bike_count = max(1, min(6, int(form.get("bike_count", [activity["expected_bike_count"]])[0])))
        except (TypeError, ValueError):
            requested_bike_count = activity["expected_bike_count"]
        if form.get("reset", [""])[0]:
            db.execute("UPDATE activities SET manual_title=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (activity_id,))
            apply_rule(db, activity_id, activity["sport_type"], None)
            notice = "Activity title returned to rule control"
            record_activity_log(db, activity_id, f"Title reset from ‘{previous_title}’ to rule control.")
        else:
            title = form.get("title", [""])[0].strip()
            if not title:
                return RedirectResponse(f"/activities/{activity_uuid}?notice=Title+is+required", status_code=303)
            db.execute("UPDATE activities SET name=?, manual_title=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (title, title, activity_id))
            notice = "Activity title updated manually"
            if title != previous_title:
                record_activity_log(db, activity_id, f"Title changed from ‘{previous_title}’ to ‘{title}’.")
            if requested_bike_count != activity["expected_bike_count"]:
                db.execute(
                    "DELETE FROM activity_bike_assignments WHERE activity_id=? AND slot_index>?",
                    (activity_id, requested_bike_count),
                )
                db.execute(
                    "UPDATE activities SET expected_bike_count=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (requested_bike_count, activity_id),
                )
                record_activity_log(
                    db,
                    activity_id,
                    f"Bike count changed from {activity['expected_bike_count']} to {requested_bike_count}.",
                )
    return RedirectResponse(f"/activities/{activity_uuid}?" + urlencode({"notice": notice}), status_code=303)


@app.post("/activities/{activity_uuid}/bikes")
async def update_activity_bikes(request: Request, activity_uuid: str):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    try:
        bike_count = max(1, int(form.get("bike_count", ["1"])[0]))
    except ValueError:
        bike_count = 1
    bike_ids=[]
    slot_values = [form.get(f"bike_slot_{index}", [""])[0] for index in range(1, bike_count + 1)]
    raw_values = slot_values if any(slot_values) else form.get("bike_ids", [])
    for raw in raw_values:
        try: bike_ids.append(int(raw))
        except ValueError: continue
    strava_updated = 0
    strava_skipped = 0
    with connection() as db:
        activity = db.execute("SELECT id, expected_bike_count FROM activities WHERE public_id=? AND deleted_at IS NULL", (activity_uuid,)).fetchone()
        if activity is None:
            return RedirectResponse("/activities?notice=Activity+not+found", status_code=303)
        activity_id = activity["id"]
        existing_assignments = db.execute(
            "SELECT bike_id FROM activity_bike_assignments WHERE activity_id=? ORDER BY slot_index", (activity_id,)
        ).fetchall()
        previous_bike_ids = [row["bike_id"] for row in existing_assignments]
        bike_rows = db.execute("SELECT id,name FROM bikes").fetchall()
        valid_ids={row["id"] for row in bike_rows}
        bike_names = {row["id"]: row["name"] for row in bike_rows}
        selected=[]
        for bike_id in bike_ids:
            if bike_id in valid_ids and bike_id not in selected: selected.append(bike_id)
        db.execute("DELETE FROM activity_bike_assignments WHERE activity_id=?", (activity_id,))
        for slot_index, bike_id in enumerate(selected[:bike_count], start=1):
            db.execute("INSERT INTO activity_bike_assignments (activity_id,bike_id,slot_index,source) VALUES (?,?,?,'MANUAL')", (activity_id,bike_id,slot_index))
        db.execute("UPDATE activities SET expected_bike_count=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (bike_count,activity_id))
        if bike_count != activity["expected_bike_count"]:
            record_activity_log(db, activity_id, f"Bike count changed from {activity['expected_bike_count']} to {bike_count}.")
        if previous_bike_ids != selected[:bike_count]:
            if selected[:bike_count]:
                labels = ", ".join(bike_names[bike_id] for bike_id in selected[:bike_count])
                record_activity_log(db, activity_id, f"Bike assignment set to {labels}.")
            else:
                record_activity_log(db, activity_id, "Bike assignment cleared.")
        if selected[:bike_count] and previous_bike_ids != selected[:bike_count]:
            # Choosing a bike here is an explicit manual decision. Mirror that
            # decision to every linked Strava activity while it is fresh,
            # instead of making the user reload and then confirm each
            # mismatch separately. The helper still safeguards activities
            # before the Garage start and only calls Strava when this Bike
            # Garage bike has a configured gear for the matching account.
            strava_updated, strava_skipped = sync_strava_activity_gears(db, activity_id)
    notice = "Bike assignment updated"
    if strava_updated:
        notice += f"; Strava bike updated for {strava_updated} activit{'ies' if strava_updated != 1 else 'y'}"
    elif strava_skipped:
        notice += "; Strava bike was not updated before Garage start"
    return RedirectResponse(f"/activities/{activity_uuid}?" + urlencode({"notice": notice}), status_code=303)


@app.post("/activities/{activity_uuid}/delete")
def delete_activity(activity_uuid: str):
    """Soft-delete an imported or test activity without losing audit history."""
    with connection() as db:
        activity = db.execute(
            "SELECT id FROM activities WHERE public_id=? AND deleted_at IS NULL",
            (activity_uuid,),
        ).fetchone()
        if activity is None:
            return RedirectResponse("/activities?notice=Activity+not+found", status_code=303)
        record_activity_log(db, activity["id"], "Activity marked as deleted.")
        db.execute(
            "UPDATE activities SET deleted_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (activity["id"],),
        )
    return RedirectResponse("/activities?notice=Activity+marked+as+deleted", status_code=303)


@app.get("/bikes")
def bikes_page(request: Request):
    return templates.TemplateResponse(request, "bikes.html", page_context(request))


@app.get("/bikes/new")
def new_bike_page(request: Request):
    user = initial_user()
    with connection() as db:
        gear_errors = refresh_strava_gear_catalog(db, user["id"])
        gears = strava_gear_options(db)
    return templates.TemplateResponse(
        request, "bike_form.html",
        page_context(request, bike=None, components={}, strava_gears=gears, strava_gear_errors=gear_errors),
    )


def save_photo(photo: UploadFile) -> str:
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    extension = allowed.get(photo.content_type or "")
    if extension is None:
        raise ValueError("Only JPEG, PNG, or WebP bike photos are supported.")
    content = photo.file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise ValueError("Bike photos must be 5 MB or smaller.")
    filename = f"{secrets.token_hex(16)}{extension}"
    (PHOTO_ROOT / filename).write_bytes(content)
    return filename


def placeholder_photo_filename() -> str:
    """A persistent synthetic image reference for bikes without an upload."""
    return f"placeholder-{secrets.token_hex(8)}.svg"


def placeholder_bike_svg(filename: str) -> str:
    """Render a detailed, neutral road-bike illustration for missing photos."""
    palettes = (("#315d50", "#a8d3bb"), ("#436a91", "#c7dbed"), ("#7b5741", "#e1c6a9"), ("#72506f", "#dfc3dd"))
    seed = sum(ord(char) for char in filename)
    dark, light = palettes[seed % len(palettes)]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 320" role="img" aria-label="Bike placeholder">
      <defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="{light}" stop-opacity=".72"/><stop offset="1" stop-color="#f7f5ef"/></linearGradient></defs>
      <rect width="480" height="320" rx="28" fill="url(#bg)"/>
      <path d="M52 270H428" stroke="{dark}" stroke-opacity=".14" stroke-width="3" stroke-linecap="round"/>
      <g fill="none" stroke="{dark}" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="126" cy="224" r="68" stroke-width="9"/><circle cx="354" cy="224" r="68" stroke-width="9"/>
        <g stroke-width="2.5" stroke-opacity=".28"><path d="M126 156v136M58 224h136M78 176l96 96M174 176 78 272M354 156v136M286 224h136M306 176l96 96M402 176l-96 96"/></g>
        <path d="M126 224 207 115 252 224H126M207 115l91 6-46 103M298 121l56 103M252 224h102" stroke-width="10"/>
        <path d="M201 107 194 91M182 91h35M298 121 315 83l30-10M315 83h31M343 73l17 8" stroke-width="9"/>
        <path d="M241 224a12 12 0 1 0 22 0 12 12 0 1 0-22 0M252 224l-20 18M252 224l21 18" stroke-width="5"/>
        <path d="M207 115 252 224M207 115l91 6" stroke="{light}" stroke-width="2.5" stroke-opacity=".9"/>
      </g>
      <circle cx="207" cy="115" r="5" fill="{dark}"/><circle cx="298" cy="121" r="5" fill="{dark}"/>
    </svg>'''


@app.post("/bikes")
async def create_bike(
    name: str | None = Form(None), identifier: str | None = Form(None), bike_type: str | None = Form(None),
    owner_name: str | None = Form(None), photo: UploadFile | None = File(None),
    starting_mileage_km: str | None = Form(None),
    shifting_ant_device_number: str | None = Form(None), bike_power_ant_device_number: str | None = Form(None),
    seatpost_ant_device_number: str | None = Form(None),
    strava_gear_ids: list[str] = Form([]),
):
    if not name or not identifier or not bike_type or not owner_name:
        return RedirectResponse(
            "/bikes/new?" + urlencode({"notice": "Name, identifier, bike type, and owner are required."}),
            status_code=303,
        )
    if bike_type not in BIKE_TYPES:
        return RedirectResponse(
            "/bikes/new?" + urlencode({"notice": "Choose Road, Gravel, or Mountainbike as the bike type."}),
            status_code=303,
        )
    try:
        photo_filename = save_photo(photo) if photo and photo.filename else placeholder_photo_filename()
        component_ant_ids = component_ant_ids_from_form(locals())
        starting_mileage_m = max(0, float(starting_mileage_km or "0") * 1000)
    except ValueError as error:
        return RedirectResponse("/bikes/new?" + urlencode({"notice": str(error)}), status_code=303)
    user = initial_user()
    with connection() as db:
        try:
            ensure_component_ids_available(db, component_ant_ids)
            validate_bike_strava_gears(db, None, strava_gear_ids)
        except ValueError as error:
            return RedirectResponse("/bikes/new?" + urlencode({"notice": str(error)}), status_code=303)
        cursor = db.execute(
            "INSERT INTO bikes (user_id, name, identifier, bike_type, owner_name, photo_filename, starting_mileage_m) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"], name.strip(), identifier.strip(), bike_type.strip(), owner_name.strip(), photo_filename, starting_mileage_m),
        )
        save_bike_components(db, cursor.lastrowid, component_ant_ids)
        save_bike_strava_gears(db, cursor.lastrowid, strava_gear_ids)
    return RedirectResponse("/bikes?notice=Bike+created", status_code=303)


@app.get("/bikes/{bike_id}")
def bike_detail(request: Request, bike_id: int):
    user = initial_user()
    mileage_entries: list[dict[str, object]] = []
    strava_gear_links: list[dict[str, object]] = []
    with connection() as db:
        bike = db.execute("SELECT * FROM bikes WHERE id = ?", (bike_id,)).fetchone()
        components = db.execute("SELECT * FROM bike_components WHERE bike_id=? ORDER BY component_type", (bike_id,)).fetchall()
        mileage_settings: dict[str, object] = {"mileage_tracking_started_at": None}
        if bike is not None:
            bike_data = dict(bike)
            mileage_settings = attach_bike_mileage(db, [bike_data], user["id"])
            bike = bike_data
            tracking_start = mileage_settings.get("mileage_tracking_started_at_epoch")
            if tracking_start is not None:
                mileage_entries = [dict(row) for row in db.execute(
                    """
                    SELECT a.id AS activity_id, a.public_id AS activity_public_id,
                           a.name, a.started_at, a.distance_m,
                           aba.slot_index, aba.source
                    FROM activity_bike_assignments aba
                    JOIN activities a ON a.id = aba.activity_id
            WHERE aba.bike_id = ? AND a.started_at_epoch >= ? AND a.deleted_at IS NULL
                    ORDER BY COALESCE(a.started_at_epoch, 0) DESC, a.id DESC, aba.slot_index ASC
                    LIMIT 10
                    """,
                    (bike_id, tracking_start),
                ).fetchall()]
            strava_gear_links = [dict(row) for row in db.execute(
                """
                SELECT sg.name, sg.external_gear_id, sg.distance_m, sg.is_primary,
                       pc.display_name AS connection_name, pc.identifier AS connection_identifier
                FROM bike_strava_gear_mappings mapping
                JOIN strava_gears sg ON sg.id=mapping.strava_gear_id
                JOIN provider_connections pc ON pc.id=sg.provider_connection_id
                WHERE mapping.bike_id=?
                ORDER BY pc.display_name COLLATE NOCASE, sg.is_primary DESC, sg.name COLLATE NOCASE
                """,
                (bike_id,),
            ).fetchall()]
    if bike is None:
        return RedirectResponse("/bikes?notice=Bike+not+found", status_code=303)
    return templates.TemplateResponse(
        request, "bike_detail.html",
        page_context(
            request, bike=bike, components=components, mileage_settings=mileage_settings,
            mileage_entries=mileage_entries, strava_gear_links=strava_gear_links,
        ),
    )


@app.get("/bikes/{bike_id}/edit")
def edit_bike_page(request: Request, bike_id: int):
    user = initial_user()
    with connection() as db:
        bike = db.execute("SELECT * FROM bikes WHERE id = ?", (bike_id,)).fetchone()
        components = bike_component_values(db, bike_id)
        gear_errors = refresh_strava_gear_catalog(db, user["id"])
        gears = strava_gear_options(db, bike_id)
    if bike is None:
        return RedirectResponse("/bikes?notice=Bike+not+found", status_code=303)
    return templates.TemplateResponse(
        request, "bike_form.html",
        page_context(request, bike=bike, components=components, strava_gears=gears, strava_gear_errors=gear_errors),
    )


@app.post("/bikes/{bike_id}/edit")
async def update_bike(
    bike_id: int, name: str | None = Form(None), identifier: str | None = Form(None), bike_type: str | None = Form(None),
    owner_name: str | None = Form(None),
    starting_mileage_km: str | None = Form(None),
    photo: UploadFile | None = File(None),
    shifting_ant_device_number: str | None = Form(None), bike_power_ant_device_number: str | None = Form(None),
    seatpost_ant_device_number: str | None = Form(None),
    strava_gear_ids: list[str] = Form([]),
):
    if not name or not identifier or not bike_type or not owner_name:
        return RedirectResponse(
            f"/bikes/{bike_id}/edit?" + urlencode({"notice": "Name, identifier, bike type, and owner are required."}),
            status_code=303,
        )
    if bike_type not in BIKE_TYPES:
        return RedirectResponse(
            f"/bikes/{bike_id}/edit?" + urlencode({"notice": "Choose Road, Gravel, or Mountainbike as the bike type."}),
            status_code=303,
        )
    try:
        starting_mileage_m = max(0, float(starting_mileage_km or "0") * 1000)
        component_ant_ids = component_ant_ids_from_form(locals())
    except ValueError:
        return RedirectResponse(
            f"/bikes/{bike_id}/edit?" + urlencode({"notice": "Starting tracked mileage must be a non-negative number."}),
            status_code=303,
        )
    with connection() as db:
        bike = db.execute("SELECT * FROM bikes WHERE id = ?", (bike_id,)).fetchone()
        if bike is None:
            return RedirectResponse("/bikes?notice=Bike+not+found", status_code=303)
        photo_filename = bike["photo_filename"]
        if photo and photo.filename:
            try:
                replacement = save_photo(photo)
            except ValueError as error:
                return RedirectResponse(f"/bikes/{bike_id}/edit?" + urlencode({"notice": str(error)}), status_code=303)
            old_path = PHOTO_ROOT / photo_filename
            if old_path.exists():
                old_path.unlink()
            photo_filename = replacement
        try:
            ensure_component_ids_available(db, component_ant_ids, exclude_bike_id=bike_id)
            validate_bike_strava_gears(db, bike_id, strava_gear_ids)
        except ValueError as error:
            return RedirectResponse(f"/bikes/{bike_id}/edit?" + urlencode({"notice": str(error)}), status_code=303)
        db.execute(
            "UPDATE bikes SET name = ?, identifier = ?, bike_type = ?, owner_name = ?, photo_filename = ?, starting_mileage_m = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (name.strip(), identifier.strip(), bike_type.strip(), owner_name.strip(), photo_filename, starting_mileage_m, bike_id),
        )
        save_bike_components(db, bike_id, component_ant_ids)
        save_bike_strava_gears(db, bike_id, strava_gear_ids)
    return RedirectResponse(f"/bikes/{bike_id}?notice=Bike+updated", status_code=303)


@app.post("/bikes/{bike_id}/delete")
def delete_bike(bike_id: int):
    with connection() as db:
        bike = db.execute("SELECT * FROM bikes WHERE id = ?", (bike_id,)).fetchone()
        if bike is None:
            return RedirectResponse("/bikes?notice=Bike+not+found", status_code=303)
        db.execute("DELETE FROM bikes WHERE id = ?", (bike_id,))
    photo_path = PHOTO_ROOT / bike["photo_filename"]
    if photo_path.exists():
        photo_path.unlink()
    return RedirectResponse("/bikes?notice=Bike+deleted", status_code=303)


@app.get("/bike-photos/{filename}")
def bike_photo(filename: str):
    if re.fullmatch(r"placeholder-[a-f0-9]{16}\.svg", filename):
        return Response(placeholder_bike_svg(filename), media_type="image/svg+xml")
    candidate = (PHOTO_ROOT / filename).resolve()
    if candidate.parent != PHOTO_ROOT.resolve() or not candidate.is_file():
        return RedirectResponse("/bikes?notice=Bike+photo+not+found", status_code=303)
    return FileResponse(candidate)


@app.post("/sync")
def sync_now():
    errors: list[str] = []
    imported = sync_all_connections(errors)
    if errors:
        notice = f"Sync failed. Unreachable providers: {'; '.join(errors)}. Imported {imported} new provider activities."
    else:
        with connection() as db:
            connected_count = db.execute("SELECT COUNT(*) FROM provider_connections WHERE provider_type IN ('STRAVA_PROXY', 'HAMMERHEAD') AND status = 'CONNECTED'").fetchone()[0]
        notice = ("Sync failed. Unreachable providers: no connected Strava Proxy or Hammerhead connection."
                  if connected_count == 0 else
                  f"Sync complete. Imported {imported} new provider activities.")
    return RedirectResponse("/activities?" + urlencode({"notice": notice}), status_code=303)


@app.get("/providers/{provider_type}/connect")
def connect_provider(request: Request, provider_type: str):
    provider = PROVIDERS.get(provider_type)
    if provider is None:
        return RedirectResponse("/providers?notice=Unknown+provider", status_code=303)

    return templates.TemplateResponse(
        request, "hammerhead_connection.html" if provider_type == "HAMMERHEAD" else "connect.html",
        page_context(request, provider_type=provider_type, provider=provider,
                     activity_types=STRAVA_ACTIVITY_TYPES, editing_hammerhead=False)
    )


@app.post("/providers/{provider_type}/connect")
async def save_provider_connection(provider_type: str, request: Request):
    if provider_type not in PROVIDERS:
        return RedirectResponse("/providers?notice=Unknown+provider", status_code=303)

    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    display_name = form.get("display_name", [""])[0].strip()
    identifier = connection_identifier(form.get("identifier", [""])[0])
    endpoint_url = form.get("endpoint_url", [""])[0].strip()
    access_token = form.get("access_token", [""])[0].strip()
    external_account_id = form.get("external_account_id", [""])[0].strip()
    activity_types = [value for value in form.get("activity_types", []) if value in STRAVA_ACTIVITY_TYPES]
    if not display_name or not CONNECTION_IDENTIFIER_PATTERN.fullmatch(identifier):
        return RedirectResponse(f"/providers/{provider_type}/connect?notice=Connection+name+and+a+valid+identifier+are+required", status_code=303)

    if provider_type == "HAMMERHEAD":
        client_id = form.get("oauth_client_id", [""])[0].strip()
        client_secret = form.get("oauth_client_secret", [""])[0].strip()
        if not client_id or not client_secret:
            return RedirectResponse(
                "/providers/HAMMERHEAD/connect?" + urlencode({"notice": "Hammerhead Client ID and Client Secret are required."}),
                status_code=303,
            )
        endpoint_url = normalise_hammerhead_base_url(endpoint_url)
        user = initial_user()
        with connection() as db:
            if not connection_identifier_is_available(db, identifier):
                return RedirectResponse("/providers/HAMMERHEAD/connect?notice=This+connection+identifier+is+already+in+use", status_code=303)
            cursor = db.execute(
                """INSERT INTO provider_connections
                   (user_id, provider_type, display_name, identifier, status, endpoint_url, oauth_client_id, oauth_client_secret, activity_types_json)
                   VALUES (?, ?, ?, ?, 'NEEDS_CONFIGURATION', ?, ?, ?, '[]')""",
                (user["id"], provider_type, display_name, identifier, endpoint_url, client_id, client_secret),
            )
            connection_id = cursor.lastrowid
        return RedirectResponse(f"/connections/{connection_id}/hammerhead/authorize", status_code=303)

    if not endpoint_url or not external_account_id:
        return RedirectResponse(
            "/providers/STRAVA_PROXY/connect?notice=Proxy+URL+and+account+identifier+are+required", status_code=303
        )
    endpoint_url = normalise_base_url(endpoint_url)
    health = health_check(endpoint_url)
    if not health.is_healthy:
        return RedirectResponse("/providers/STRAVA_PROXY/connect?" + urlencode({"notice": health.message}), status_code=303)

    user = initial_user()
    status = "CONNECTED"
    with connection() as db:
        if not connection_identifier_is_available(db, identifier):
            return RedirectResponse("/providers/STRAVA_PROXY/connect?notice=This+connection+identifier+is+already+in+use", status_code=303)
        db.execute(
            """
            INSERT INTO provider_connections
              (user_id, provider_type, display_name, identifier, status, endpoint_url, access_token, external_account_id, activity_types_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], provider_type, display_name, identifier, status, endpoint_url or None,
             access_token or None, external_account_id or None, json.dumps(activity_types)),
        )
    return RedirectResponse("/providers?notice=Provider+connection+saved", status_code=303)


@app.post("/connections/{connection_id}/test")
def test_provider_connection(connection_id: int):
    with connection() as db:
        provider_connection = db.execute(
            "SELECT * FROM provider_connections WHERE id = ?", (connection_id,)
        ).fetchone()
        if provider_connection is None:
            return RedirectResponse("/providers?notice=Provider+connection+not+found", status_code=303)
        result = (test_hammerhead_connection if provider_connection["provider_type"] == "HAMMERHEAD" else test_connection)(
            provider_connection["endpoint_url"] or "",
            provider_connection["access_token"],
            provider_connection["external_account_id"] or "",
        )
        db.execute(
            """
            UPDATE provider_connections
            SET status = ?, last_tested_at = CURRENT_TIMESTAMP, last_test_status = ?,
                last_test_message = ?, last_test_activity_count = ?, last_test_activities_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                "CONNECTED" if result.is_healthy else "NEEDS_CONFIGURATION",
                "SUCCESS" if result.is_healthy else "FAILED",
                result.message,
                result.activity_count,
                json.dumps(result.activities),
                connection_id,
            ),
        )
    return RedirectResponse("/providers?" + urlencode({"test_connection": connection_id}), status_code=303)


@app.post("/connections/{connection_id}/delete")
def delete_provider_connection(connection_id: int):
    """Remove a connection and its imported source records.

    Canonical activities that are shared by another provider remain intact.
    Activities that lose their final source are removed with assignments.
    """
    with connection() as db:
        if db.execute("SELECT 1 FROM provider_connections WHERE id = ?", (connection_id,)).fetchone() is None:
            return RedirectResponse("/providers?notice=Provider+connection+not+found", status_code=303)
        activity_ids = [row[0] for row in db.execute(
            """SELECT DISTINCT apl.activity_id FROM activity_provider_links apl
               JOIN provider_activities pa ON pa.id = apl.provider_activity_id
               WHERE pa.connection_id = ?""", (connection_id,)
        ).fetchall()]
        db.execute("""DELETE FROM activity_provider_links WHERE provider_activity_id IN
                   (SELECT id FROM provider_activities WHERE connection_id = ?)""", (connection_id,))
        db.execute("DELETE FROM provider_activities WHERE connection_id = ?", (connection_id,))
        orphan_ids = [activity_id for activity_id in activity_ids if not db.execute(
            "SELECT 1 FROM activity_provider_links WHERE activity_id = ? LIMIT 1", (activity_id,)
        ).fetchone()]
        if orphan_ids:
            placeholders = ",".join("?" for _ in orphan_ids)
            db.execute(f"DELETE FROM activity_bike_assignments WHERE activity_id IN ({placeholders})", orphan_ids)
            db.execute(f"DELETE FROM activities WHERE id IN ({placeholders})", orphan_ids)
        db.execute("DELETE FROM provider_connections WHERE id = ?", (connection_id,))
    return RedirectResponse("/providers?notice=Provider+connection+deleted", status_code=303)


@app.get("/connections/{connection_id}/hammerhead/authorize")
def authorize_hammerhead_connection(request: Request, connection_id: int):
    with connection() as db:
        provider_connection = db.execute("SELECT * FROM provider_connections WHERE id = ?", (connection_id,)).fetchone()
        if provider_connection is None or provider_connection["provider_type"] != "HAMMERHEAD":
            return RedirectResponse("/providers?notice=Provider+connection+not+found", status_code=303)
        state = secrets.token_urlsafe(32)
        db.execute("UPDATE provider_connections SET oauth_state=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (state, connection_id))
    redirect_uri = str(request.base_url).rstrip("/") + "/providers/HAMMERHEAD/callback"
    return RedirectResponse(hammerhead_authorization_url(provider_connection["oauth_client_id"], redirect_uri, state), status_code=303)


@app.get("/providers/HAMMERHEAD/callback")
def hammerhead_oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse("/providers?" + urlencode({"notice": f"Hammerhead authorization was declined: {error}."}), status_code=303)
    if not code or not state:
        return RedirectResponse("/providers?notice=Hammerhead+authorization+did+not+return+a+code.", status_code=303)
    with connection() as db:
        provider_connection = db.execute("SELECT * FROM provider_connections WHERE provider_type='HAMMERHEAD' AND oauth_state=?", (state,)).fetchone()
    if provider_connection is None:
        return RedirectResponse("/providers?notice=Hammerhead+authorization+state+was+not+recognized.", status_code=303)
    redirect_uri = str(request.base_url).rstrip("/") + "/providers/HAMMERHEAD/callback"
    try:
        token = exchange_hammerhead_code(provider_connection["oauth_client_id"], provider_connection["oauth_client_secret"], code, redirect_uri)
    except Exception as exc:
        return RedirectResponse("/providers?" + urlencode({"notice": f"Hammerhead token exchange failed: {exc}"}), status_code=303)
    expires_at = int(time.time()) + int(token.get("expires_in") or 0)
    with connection() as db:
        db.execute("""UPDATE provider_connections SET access_token=?, refresh_token=?, token_expires_at=?, external_account_id=?,
                   oauth_state=NULL, status='NEEDS_CONFIGURATION', updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                   (token["access_token"], token.get("refresh_token"), expires_at, str(token.get("user_id") or ""), provider_connection["id"]))
    return RedirectResponse("/providers?" + urlencode({"notice": "Hammerhead authorization completed. Run Test connection to verify the activity feed."}), status_code=303)


@app.get("/connections/{connection_id}/edit")
def edit_provider_connection(request: Request, connection_id: int):
    with connection() as db:
        provider_connection = db.execute(
            "SELECT * FROM provider_connections WHERE id = ?", (connection_id,)
        ).fetchone()
    if provider_connection is None or provider_connection["provider_type"] not in PROVIDERS:
        return RedirectResponse("/providers?notice=Provider+connection+not+found", status_code=303)
    provider_connection = dict(provider_connection)
    try:
        provider_connection["activity_types"] = json.loads(provider_connection.get("activity_types_json") or "[]")
    except json.JSONDecodeError:
        provider_connection["activity_types"] = []
    template_name = "hammerhead_connection.html" if provider_connection["provider_type"] == "HAMMERHEAD" else "edit_connection.html"
    return templates.TemplateResponse(
        request,
        template_name,
        page_context(
            request,
            provider=PROVIDERS[provider_connection["provider_type"]],
            provider_connection=provider_connection,
            activity_types=STRAVA_ACTIVITY_TYPES,
            editing_hammerhead=provider_connection["provider_type"] == "HAMMERHEAD",
        ),
    )


@app.post("/connections/{connection_id}/edit")
async def save_edited_provider_connection(connection_id: int, request: Request):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    display_name = form.get("display_name", [""])[0].strip()
    identifier = connection_identifier(form.get("identifier", [""])[0])
    endpoint_url = normalise_base_url(form.get("endpoint_url", [""])[0])
    external_account_id = form.get("external_account_id", [""])[0].strip()
    replacement_api_key = form.get("access_token", [""])[0].strip()
    activity_types = [value for value in form.get("activity_types", []) if value in STRAVA_ACTIVITY_TYPES]
    with connection() as db:
        provider_connection = db.execute("SELECT * FROM provider_connections WHERE id = ?", (connection_id,)).fetchone()
    if provider_connection is None or provider_connection["provider_type"] not in PROVIDERS:
        return RedirectResponse("/providers?notice=Provider+connection+not+found", status_code=303)
    with connection() as db:
        if not connection_identifier_is_available(db, identifier, exclude_id=connection_id):
            return RedirectResponse(f"/connections/{connection_id}/edit?notice=Choose+a+unique+identifier+using+lowercase+letters%2C+numbers%2C+hyphens%2C+or+underscores", status_code=303)
    if provider_connection["provider_type"] == "HAMMERHEAD":
        client_id = form.get("oauth_client_id", [""])[0].strip()
        client_secret = form.get("oauth_client_secret", [""])[0].strip() or provider_connection["oauth_client_secret"]
        if not display_name or not client_id or not client_secret:
            return RedirectResponse(f"/connections/{connection_id}/edit?" + urlencode({"notice": "Connection name, Hammerhead Client ID, and Client Secret are required."}), status_code=303)
        with connection() as db:
            db.execute("""UPDATE provider_connections SET display_name=?, identifier=?, endpoint_url=?, oauth_client_id=?, oauth_client_secret=?,
                       status='NEEDS_CONFIGURATION', last_tested_at=NULL, last_test_status=NULL, last_test_message=NULL,
                       last_test_activity_count=NULL, last_test_activities_json=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                       (display_name, identifier, normalise_hammerhead_base_url(form.get("endpoint_url", [DEFAULT_API_BASE_URL])[0]), client_id, client_secret, connection_id))
        return RedirectResponse(f"/connections/{connection_id}/hammerhead/authorize", status_code=303)
    if not display_name or not endpoint_url or not external_account_id:
        return RedirectResponse(
            f"/connections/{connection_id}/edit?notice=Connection+name%2C+proxy+URL%2C+and+account+identifier+are+required",
            status_code=303,
        )

    with connection() as db:
        provider_connection = db.execute(
            "SELECT * FROM provider_connections WHERE id = ?", (connection_id,)
        ).fetchone()
        if provider_connection is None or provider_connection["provider_type"] != "STRAVA_PROXY":
            return RedirectResponse("/providers?notice=Provider+connection+not+found", status_code=303)
        api_key = replacement_api_key or provider_connection["access_token"]
        db.execute(
            """
            UPDATE provider_connections
            SET display_name = ?, identifier = ?, endpoint_url = ?, external_account_id = ?, access_token = ?,
                activity_types_json = ?,
                status = ?, last_tested_at = NULL, last_test_status = NULL,
                last_test_message = NULL, last_test_activity_count = NULL,
                last_test_activities_json = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (display_name, identifier, endpoint_url, external_account_id, api_key, json.dumps(activity_types),
             provider_connection["status"], connection_id),
        )
    return RedirectResponse("/providers?notice=Connection+updated.+Existing+status+was+retained.+Run+a+test+when+convenient.", status_code=303)
