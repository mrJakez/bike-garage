from __future__ import annotations

import json
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import os
from queue import Queue
import secrets
import re
from threading import Thread
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .connectors.strava_proxy import fetch_activity as fetch_strava_activity, fetch_bikes as fetch_strava_bikes, health_check, normalise_base_url, test_connection, update_activity as update_strava_activity
from .connectors.hammerhead import DEFAULT_API_BASE_URL, authorization_url as hammerhead_authorization_url, exchange_code as exchange_hammerhead_code, fetch_activity_detail as fetch_hammerhead_activity_detail, fetch_activity_fit as fetch_hammerhead_activity_fit, normalise_base_url as normalise_hammerhead_base_url, test_connection as test_hammerhead_connection
from .database import connection, initial_user, initialise_database
from .auth import complete_authentication, complete_registration, has_passkey, issue_authentication_options, issue_registration_options, registration_enabled
from .sync import ActivityContext, apply_rule, attach_hardware_observations, expression_error, evaluate_expression, notify_scheduler_settings_changed, refresh_all_canonical_timings, refresh_hammerhead_connection_token, repair_strava_local_start_times, scheduler_configuration, start_sync_loop, store_fit_hardware, sync_all_connections, validate_expression


APP_ROOT = Path(__file__).parent
PHOTO_ROOT = Path(os.getenv("DATABASE_PATH", "data/bike-garage.db")).parent / "bike-photos"
PHOTO_ROOT.mkdir(parents=True, exist_ok=True)
SESSION_SECRET_PATH = PHOTO_ROOT.parent / ".bike-garage-session-secret"
templates = Jinja2Templates(directory=str(APP_ROOT / "templates"))
LOCAL_TIMEZONE = ZoneInfo("Europe/Berlin")


def ghcr_build_metadata() -> dict[str, str] | None:
    """Provide published-image provenance, or a clear local-development marker."""
    if os.getenv("BIKE_GARAGE_BUILD_SOURCE") == "ghcr":
        revision = os.getenv("BIKE_GARAGE_BUILD_GIT_SHA", "").strip()
        raw_committed_at = os.getenv("BIKE_GARAGE_BUILD_COMMIT_DATE", "").strip()
        if not revision or not raw_committed_at:
            return None
        try:
            committed_at = datetime.fromisoformat(raw_committed_at.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    else:
        revision = "DEV"
        committed_at = datetime.now(UTC)
    return {
        "revision": revision[:7],
        "source": "Development build" if revision == "DEV" else "Published build",
        "committed_at": committed_at.strftime("%Y-%m-%d %H:%M UTC"),
        "committed_at_iso": committed_at.isoformat().replace("+00:00", "Z"),
    }


def public_origin(request: Request) -> str:
    """Use the explicit external HTTPS origin when the app is behind a proxy."""
    return os.getenv("BIKE_GARAGE_PUBLIC_ORIGIN", "").strip().rstrip("/") or str(request.base_url).rstrip("/")


def passkey_login_disabled() -> bool:
    """Allow an explicitly trusted local deployment to run without a login."""
    return os.getenv("BIKE_GARAGE_AUTH_DISABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def session_secret() -> str:
    """Keep local sessions valid across rebuilds when no explicit secret is configured."""
    configured = os.getenv("BIKE_GARAGE_SESSION_SECRET", "").strip()
    if configured:
        return configured
    try:
        return SESSION_SECRET_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        secret = secrets.token_urlsafe(48)
        try:
            descriptor = os.open(SESSION_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return SESSION_SECRET_PATH.read_text(encoding="utf-8").strip()
        with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
            secret_file.write(secret)
        return secret


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


def activity_datetime_part(value: object, pattern: str, fallback: str) -> str:
    """Format one human-readable part of a provider activity timestamp."""
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(LOCAL_TIMEZONE)
        return parsed.strftime(pattern)
    except (TypeError, ValueError):
        return fallback


def format_activity_date(value: object) -> str:
    return activity_datetime_part(value, "%d.%m.%Y", "Unknown date")


def format_activity_time(value: object) -> str:
    return activity_datetime_part(value, "%H:%M", "—")


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
templates.env.filters["activity_date"] = format_activity_date
templates.env.filters["activity_time"] = format_activity_time
templates.env.filters["log_datetime"] = format_log_datetime
templates.env.filters["km"] = format_kilometres
app = FastAPI(title="Bike Garage")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")


@app.middleware("http")
async def require_passkey(request: Request, call_next):
    """Require a passkey only after the single-user account is initialized."""
    if passkey_login_disabled():
        return await call_next(request)
    public_paths = {"/login", "/register", "/auth/login/options", "/auth/login/verify", "/auth/register/options", "/auth/register/verify"}
    if request.url.path.startswith("/static/") or request.url.path in public_paths:
        return await call_next(request)
    with connection() as db:
        configured = has_passkey(db)
    if configured and not request.session.get("authenticated"):
        if request.method == "GET":
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Passkey sign-in required."}, status_code=401)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    max_age=60 * 60 * 24 * 3650,
    same_site="lax",
    https_only=os.getenv("BIKE_GARAGE_PASSKEY_ORIGIN", "").startswith("https://"),
)

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
BIKE_STRAVA_ACTIVITY_TYPES = (
    ("Ride", "Radfahrt"),
    ("GravelRide", "Schotterfahrt"),
    ("EBikeRide", "E-Bike-Radfahrt"),
    ("MountainBikeRide", "Mountainbike-Fahrt"),
)
BIKE_STRAVA_ACTIVITY_TYPE_VALUES = frozenset(value for value, _ in BIKE_STRAVA_ACTIVITY_TYPES)
BIKE_STRAVA_ACTIVITY_TYPE_LABELS = dict(BIKE_STRAVA_ACTIVITY_TYPES)
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


def strava_activity_mismatches(db: object, activity_id: int) -> list[dict[str, object]]:
    """Return Strava sources whose configured bike or activity type differs."""
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
        """SELECT pa.id AS provider_activity_id, pa.external_activity_id, pa.sport_type AS current_sport_type, pa.raw_json,
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
            """SELECT sg.external_gear_id, sg.name, b.strava_activity_type FROM activity_bike_assignments aba
               JOIN bike_strava_gear_mappings mapping ON mapping.bike_id=aba.bike_id
               JOIN bikes b ON b.id=aba.bike_id
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
        current_sport_type = str(payload.get("sport_type") or row["current_sport_type"] or "")
        gear_mismatch = current_gear_id != mapping["external_gear_id"]
        desired_sport_type = mapping["strava_activity_type"]
        activity_type_mismatch = bool(desired_sport_type and current_sport_type != desired_sport_type)
        if not gear_mismatch and not activity_type_mismatch:
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
            "gear_mismatch": gear_mismatch,
            "desired_sport_type": desired_sport_type,
            "desired_sport_type_label": BIKE_STRAVA_ACTIVITY_TYPE_LABELS.get(desired_sport_type, desired_sport_type),
            "current_sport_type": current_sport_type or "Unknown type",
            "current_sport_type_label": BIKE_STRAVA_ACTIVITY_TYPE_LABELS.get(current_sport_type, current_sport_type or "Unknown type"),
            "activity_type_mismatch": activity_type_mismatch,
            "update_allowed": garage_start_epoch is not None and (activity["started_at_epoch"] or 0) >= garage_start_epoch,
        })
    return mismatches


def sync_strava_activity(db: object, activity_id: int, provider_activity_id: int | None = None) -> tuple[int, int, int]:
    """Mirror configured Bike Garage bike fields to the matching Strava source."""
    updated_count = 0
    skipped_count = 0
    failed_count = 0
    for row in strava_activity_mismatches(db, activity_id):
        if provider_activity_id is not None and row["provider_activity_id"] != provider_activity_id:
            continue
        if not row["update_allowed"]:
            skipped_count += 1
            record_activity_log(
                db, activity_id,
                f"Strava update skipped for {row['connection_name']}: activity is before Garage start.",
                logger="STRAVA",
            )
            continue
        try:
            current_payload = json.loads(row["raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            current_payload = {}
        changes = []
        if row["gear_mismatch"]:
            changes.append(f"bike {row['desired_gear_name']} (Before: {row['current_gear_name']})")
        if row["activity_type_mismatch"]:
            changes.append(f"activity type {row['desired_sport_type']} (Before: {row['current_sport_type']})")
        note = "Bike Garage: " + "; ".join(changes)
        existing_description = str(current_payload.get("description") or "").strip()
        # Do not erase a rider's own activity notes; append one concise audit
        # line for each actual Bike Garage correction instead.
        description = existing_description if note in existing_description else (
            f"{existing_description}\n\n{note}" if existing_description else note
        )
        try:
            updated = update_strava_activity(
                row["endpoint_url"], row["access_token"], row["external_account_id"],
                row["external_activity_id"],
                gear_id=row["desired_gear_id"] if row["gear_mismatch"] else None,
                sport_type=row["desired_sport_type"] if row["activity_type_mismatch"] else None,
                description=description,
            )
        except Exception as error:
            failed_count += 1
            record_activity_log(
                db, activity_id,
                f"Strava update failed for {row['connection_name']}: {error}",
                logger="STRAVA",
            )
            continue
        payload = current_payload
        payload.update(updated)
        if row["gear_mismatch"]:
            payload["gear_id"] = row["desired_gear_id"]
        if row["activity_type_mismatch"]:
            payload["sport_type"] = row["desired_sport_type"]
        payload["description"] = description
        db.execute(
            "UPDATE provider_activities SET raw_json=?, sport_type=COALESCE(?, sport_type) WHERE id=?",
            (json.dumps(payload), row["desired_sport_type"] if row["activity_type_mismatch"] else None, row["provider_activity_id"]),
        )
        record_activity_log(db, activity_id, f"Strava updated for {row['connection_name']}: {note.removeprefix('Bike Garage: ')}.", logger="STRAVA")
        updated_count += 1
    return updated_count, skipped_count, failed_count


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
    seconds = duration_seconds(value, milliseconds=milliseconds)
    if seconds is None:
        return "—"
    return str(timedelta(seconds=seconds))


def duration_seconds(value: object, *, milliseconds: bool = False) -> int | None:
    try:
        return max(0, int(float(value) / 1000) if milliseconds else int(float(value)))
    except (TypeError, ValueError):
        return None


def activity_time_range(value: object, duration: int | None) -> str:
    """Render a local start–end time range for compact activity cards."""
    if not value:
        return "—"
    try:
        started_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if started_at.tzinfo is not None:
            started_at = started_at.astimezone(LOCAL_TIMEZONE)
        start = started_at.strftime("%H:%M")
        return f"{start}–{(started_at + timedelta(seconds=duration)).strftime('%H:%M')}" if duration is not None else start
    except (TypeError, ValueError):
        return format_activity_time(value)


def provider_duration(payload: dict[str, object], provider_type: str | None) -> object:
    """Read a provider duration from the normalized payload or its raw source."""
    if provider_type == "HAMMERHEAD":
        duration = payload.get("duration")
        if duration is not None:
            return duration
        raw_hammerhead = payload.get("raw_hammerhead")
        return raw_hammerhead.get("duration") if isinstance(raw_hammerhead, dict) else None
    return payload.get("moving_time") or payload.get("elapsed_time")


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
        clauses.insert(0, "a.deleted_at IS NULL")
        where_clause = f"WHERE {' AND '.join(clauses)}"
        activity_result_summary = None
        if not filters.get("todo"):
            activity_result_summary = dict(db.execute(
                f"""
                SELECT COUNT(*) AS activity_count,
                       COALESCE(SUM(a.distance_m), 0) AS total_distance_m
                FROM activities a
                {where_clause}
                """,
                values,
            ).fetchone())
        activity_limit = "" if filters.get("todo") else "LIMIT 100"
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
            {activity_limit}
            """,
            values,
        ).fetchall()]
        # The stream needs a compact, view-only route preview and a useful
        # duration without loading a full provider detail page per card.
        provider_data: dict[int, list[dict[str, object]]] = {}
        for row in db.execute(
            """
            SELECT apl.activity_id, pa.id AS provider_activity_id, pa.name AS provider_activity_title,
                   pa.raw_json, pc.provider_type, pc.identifier AS connection_identifier,
                   pc.display_name AS connection_name
            FROM activity_provider_links apl
            JOIN provider_activities pa ON pa.id = apl.provider_activity_id
            JOIN provider_connections pc ON pc.id = pa.connection_id
            """
        ).fetchall():
            provider_data.setdefault(row["activity_id"], []).append(dict(row))
        for activity in activities:
            activity["route_polyline"] = None
            activity["duration_label"] = "—"
            activity["time_range"] = format_activity_time(activity["started_at"])
            # Keep one entry per linked source: two Strava accounts should
            # deliberately appear as two Strava marks in the stream.
            activity["provider_activities"] = [
                {
                    "provider": str(provider["provider_type"]),
                    "connection": str(provider["connection_identifier"] or provider["connection_name"] or "Unknown provider"),
                    "title": str(provider["provider_activity_title"] or "Unnamed activity"),
                }
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
                    raw_duration = provider_duration(payload, provider["provider_type"])
                    seconds = duration_seconds(raw_duration, milliseconds=provider["provider_type"] == "HAMMERHEAD")
                    activity["duration_label"] = duration_label(
                        raw_duration, milliseconds=provider["provider_type"] == "HAMMERHEAD"
                    )
                    activity["time_range"] = activity_time_range(activity["started_at"], seconds)
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
            activity["strava_mismatches"] = strava_activity_mismatches(db, activity["id"])
            activity["strava_gear_todo"] = any(item["gear_mismatch"] for item in activity["strava_mismatches"])
            activity["strava_activity_type_todo"] = any(
                item["activity_type_mismatch"] for item in activity["strava_mismatches"]
            )
            activity["strava_todo"] = bool(activity["strava_mismatches"])
            activity["bike_assignment_todo"] = (
                not expected_count
                or len(assigned_bikes) != expected_count
                or activity["strava_todo"]
            )
        attention_activities = [activity for activity in activities if activity["bike_assignment_todo"]]
        if filters.get("todo"):
            activity_result_summary = {
                "activity_count": len(attention_activities),
                "total_distance_m": sum(float(activity.get("distance_m") or 0) for activity in attention_activities),
            }
            activities = attention_activities[:100]
            attention_activities = activities
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
        "build_metadata": ghcr_build_metadata(),
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
            apply_rule(db, activity["id"], activity["sport_type"], None, force=True)
        except Exception:
            failures += 1
    return len(activities), failures


@app.get("/login")
def login_page(request: Request):
    if passkey_login_disabled():
        return RedirectResponse("/", status_code=303)
    with connection() as db:
        configured = has_passkey(db)
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=303)
    if not configured and registration_enabled():
        return RedirectResponse("/register", status_code=303)
    return templates.TemplateResponse(request, "passkey_login.html", {"configured": configured})


@app.get("/register")
def register_passkey_page(request: Request):
    if passkey_login_disabled():
        return RedirectResponse("/", status_code=303)
    if not registration_enabled():
        return RedirectResponse("/login?notice=Passkey+registration+is+disabled", status_code=303)
    return templates.TemplateResponse(request, "passkey_register.html", {})


@app.post("/auth/register/options")
def register_passkey_options(request: Request):
    if not registration_enabled():
        return JSONResponse({"detail": "Passkey registration is disabled."}, status_code=403)
    return Response(issue_registration_options(request, initial_user()), media_type="application/json")


@app.post("/auth/register/verify")
async def register_passkey_verify(request: Request):
    if not registration_enabled():
        return JSONResponse({"detail": "Passkey registration is disabled."}, status_code=403)
    try:
        credential_id, public_key, sign_count = complete_registration(request, await request.json(), initial_user())
    except Exception as error:
        return JSONResponse({"detail": f"Passkey registration failed: {error}"}, status_code=400)
    user = initial_user()
    with connection() as db:
        db.execute(
            """INSERT INTO passkeys (user_id,credential_id,credential_public_key,sign_count)
               VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET credential_id=excluded.credential_id,
               credential_public_key=excluded.credential_public_key,sign_count=excluded.sign_count,
               created_at=CURRENT_TIMESTAMP,last_used_at=NULL""",
            (user["id"], credential_id, public_key, sign_count),
        )
        db.execute("UPDATE users SET login_enabled=1 WHERE id=?", (user["id"],))
    request.session["authenticated"] = True
    return JSONResponse({"ok": True})


@app.post("/auth/login/options")
def login_passkey_options(request: Request):
    with connection() as db:
        passkey = db.execute("SELECT * FROM passkeys LIMIT 1").fetchone()
    if passkey is None:
        return JSONResponse({"detail": "No passkey is configured."}, status_code=404)
    return Response(issue_authentication_options(request, passkey), media_type="application/json")


@app.post("/auth/login/verify")
async def login_passkey_verify(request: Request):
    with connection() as db:
        passkey = db.execute("SELECT * FROM passkeys LIMIT 1").fetchone()
        if passkey is None:
            return JSONResponse({"detail": "No passkey is configured."}, status_code=404)
        try:
            sign_count = complete_authentication(request, await request.json(), passkey)
        except Exception as error:
            return JSONResponse({"detail": f"Passkey sign-in failed: {error}"}, status_code=401)
        db.execute("UPDATE passkeys SET sign_count=?,last_used_at=CURRENT_TIMESTAMP WHERE id=?", (sign_count, passkey["id"]))
    request.session["authenticated"] = True
    return JSONResponse({"ok": True})


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def month_boundary(reference: datetime, offset: int = 0) -> datetime:
    """Return the local midnight at the first day of a relative month."""
    absolute_month = reference.month - 1 + offset
    return datetime(reference.year + absolute_month // 12, absolute_month % 12 + 1, 1, tzinfo=LOCAL_TIMEZONE)


def dashboard_monthly_bike_stats(user_id: int) -> dict[str, object]:
    """Summarise managed, bike-assigned riding for the dashboard overview."""
    now = datetime.now(LOCAL_TIMEZONE)
    with connection() as db:
        settings = db.execute(
            "SELECT mileage_tracking_started_at_epoch FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        garage_start = int(settings["mileage_tracking_started_at_epoch"]) if settings and settings["mileage_tracking_started_at_epoch"] is not None else None
        current_month = month_boundary(now)
        if garage_start is None:
            month_starts = [month_boundary(current_month, offset) for offset in range(-5, 1)]
        else:
            first_managed_month = month_boundary(datetime.fromtimestamp(garage_start, LOCAL_TIMEZONE))
            managed_month_count = (current_month.year - first_managed_month.year) * 12 + current_month.month - first_managed_month.month + 1
            visible_month_count = max(0, min(6, managed_month_count))
            first_visible_month = month_boundary(first_managed_month, max(0, managed_month_count - 6))
            month_starts = [month_boundary(first_visible_month, offset) for offset in range(visible_month_count)]
            while len(month_starts) < 6:
                month_starts.append(month_boundary(month_starts[-1] if month_starts else current_month, 1))
        months: list[dict[str, object]] = []
        for start in month_starts:
            end = month_boundary(start, 1)
            lower_bound = max(int(start.timestamp()), garage_start or 0)
            row = db.execute(
                """SELECT COALESCE(SUM(a.distance_m),0) AS distance_m,COUNT(*) AS ride_count
                   FROM activities a WHERE a.deleted_at IS NULL
                     AND COALESCE(a.started_at_epoch,0)>=? AND COALESCE(a.started_at_epoch,0)<?
                     AND EXISTS (SELECT 1 FROM activity_bike_assignments aba WHERE aba.activity_id=a.id)""",
                (lower_bound, int(end.timestamp())),
            ).fetchone()
            bike_rows = db.execute(
                """SELECT b.id,b.name,b.photo_filename,b.colour,COALESCE(SUM(a.distance_m),0) AS distance_m,
                          COUNT(DISTINCT a.id) AS ride_count
                   FROM bikes b
                   JOIN activity_bike_assignments aba ON aba.bike_id=b.id
                   JOIN activities a ON a.id=aba.activity_id
                   WHERE b.user_id=? AND a.deleted_at IS NULL
                     AND COALESCE(a.started_at_epoch,0)>=? AND COALESCE(a.started_at_epoch,0)<?
                   GROUP BY b.id HAVING COALESCE(SUM(a.distance_m),0)>0
                   ORDER BY distance_m DESC,b.name COLLATE NOCASE""",
                (user_id, lower_bound, int(end.timestamp())),
            ).fetchall()
            total_distance_m = float(row["distance_m"] or 0)
            bikes = []
            for bike_row in bike_rows:
                distance_m = float(bike_row["distance_m"] or 0)
                bikes.append({
                    **dict(bike_row), "distance_m": distance_m,
                    "share_percent": round(distance_m / total_distance_m * 100) if total_distance_m else 0,
                    "chart_colour": bike_colour(bike_row["colour"]),
                })
            months.append({
                "key": start.strftime("%Y-%m"), "label": start.strftime("%b"),
                "full_label": start.strftime("%B %Y"), "distance_m": total_distance_m,
                "ride_count": int(row["ride_count"] or 0), "active_bike_count": len(bikes), "bikes": bikes,
                "selectable": start <= current_month, "is_current": start == current_month,
            })
    selected_month = next((month for month in months if month["key"] == current_month.strftime("%Y-%m")), months[-1])
    max_month_distance = max((float(month["distance_m"]) for month in months), default=0.0)
    managed_months = [month for month in months if month["selectable"]]
    average_month_distance_m = sum(float(month["distance_m"]) for month in managed_months) / len(managed_months) if managed_months else 0.0
    for month in months:
        month["height_percent"] = round(float(month["distance_m"]) / max_month_distance * 100) if max_month_distance else 0
    return {
        "label": selected_month["full_label"],
        "total_distance_m": selected_month["distance_m"],
        "ride_count": selected_month["ride_count"],
        "active_bike_count": selected_month["active_bike_count"],
        "bikes": selected_month["bikes"],
        "months": months,
        "average_month_distance_m": average_month_distance_m,
        "average_height_percent": round(average_month_distance_m / max_month_distance * 100) if max_month_distance else 0,
        "has_rides": float(selected_month["distance_m"] or 0) > 0,
    }


@app.get("/")
def dashboard(request: Request):
    user = initial_user()
    return templates.TemplateResponse(
        request, "dashboard.html", page_context(request, monthly_bike_stats=dashboard_monthly_bike_stats(user["id"]))
    )


@app.get("/providers")
def providers(request: Request):
    return templates.TemplateResponse(request, "providers.html", page_context(request))


PROVIDER_TRANSFER_FIELDS = (
    "provider_type", "display_name", "identifier", "endpoint_url", "access_token",
    "external_account_id", "oauth_client_id", "oauth_client_secret", "refresh_token",
    "token_expires_at",
)


def provider_transfer_record(row: object) -> dict[str, object]:
    """Return the portable, credential-bearing portion of a provider connection."""
    record = {field: row[field] for field in PROVIDER_TRANSFER_FIELDS}
    try:
        activity_types = json.loads(row["activity_types_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        activity_types = []
    record["activity_types"] = activity_types if isinstance(activity_types, list) else []
    return record


def connection_can_be_activated(row: object) -> bool:
    """Only activate a saved connection when it still has the credentials to sync."""
    if not row["endpoint_url"]:
        return False
    if row["provider_type"] == "STRAVA_PROXY":
        return bool(row["access_token"])
    if row["provider_type"] == "HAMMERHEAD":
        return bool(row["oauth_client_id"] and row["oauth_client_secret"] and (row["access_token"] or row["refresh_token"]))
    return False


@app.post("/settings/providers/export")
def export_provider_connections():
    """Download the credential-bearing provider configuration for a trusted move."""
    user = initial_user()
    with connection() as db:
        rows = db.execute(
            """SELECT * FROM provider_connections
               WHERE user_id=? AND provider_type IN ('STRAVA_PROXY', 'HAMMERHEAD')
               ORDER BY id""",
            (user["id"],),
        ).fetchall()
        if not rows:
            return RedirectResponse("/settings?notice=No+saved+provider+connections+to+export", status_code=303)
        export_document = {
            "format": "bike-garage-provider-connections",
            "version": 1,
            "exported_at": datetime.now(UTC).isoformat(),
            "connections": [provider_transfer_record(row) for row in rows],
        }
    filename = f"bike-garage-connections-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=json.dumps(export_document, indent=2) + "\n",
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/settings/providers/import")
async def import_provider_connections(archive: UploadFile = File(...)):
    """Import a previously exported provider credential archive and activate it here."""
    try:
        raw_document = await archive.read()
        if len(raw_document) > 1_000_000:
            raise ValueError("The file is too large.")
        document = json.loads(raw_document.decode("utf-8"))
        records = document.get("connections") if isinstance(document, dict) else None
        if not isinstance(document, dict) or document.get("format") != "bike-garage-provider-connections" or document.get("version") != 1:
            raise ValueError("This is not a Bike Garage provider-connections export.")
        if not isinstance(records, list) or not records:
            raise ValueError("The export does not contain any connections.")
        if len(records) > 100:
            raise ValueError("The export contains too many connections.")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return RedirectResponse("/settings?" + urlencode({"notice": f"Import failed: {error}"}), status_code=303)

    imported: list[dict[str, object]] = []
    identifiers: set[str] = set()
    try:
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Each connection must be an object.")
            provider_type = record.get("provider_type")
            display_name = record.get("display_name")
            identifier = record.get("identifier")
            if provider_type not in {"STRAVA_PROXY", "HAMMERHEAD"}:
                raise ValueError("An export contains an unsupported provider.")
            if not isinstance(display_name, str) or not display_name.strip():
                raise ValueError("Every connection needs a name.")
            if not isinstance(identifier, str) or not CONNECTION_IDENTIFIER_PATTERN.fullmatch(identifier):
                raise ValueError("Every connection needs a valid identifier.")
            if identifier in identifiers:
                raise ValueError("The export contains the same identifier more than once.")
            identifiers.add(identifier)
            activity_types = record.get("activity_types") or []
            if not isinstance(activity_types, list) or any(
                not isinstance(item, str) or item not in STRAVA_ACTIVITY_TYPES for item in activity_types
            ):
                raise ValueError("An export contains invalid activity types.")
            copied = {field: record.get(field) for field in PROVIDER_TRANSFER_FIELDS}
            if any(value is not None and not isinstance(value, str) for field, value in copied.items() if field != "token_expires_at"):
                raise ValueError("An export contains an invalid connection value.")
            expires_at = copied["token_expires_at"]
            if expires_at is not None and not isinstance(expires_at, int):
                raise ValueError("An export contains an invalid token expiry.")
            copied["display_name"] = display_name.strip()
            copied["identifier"] = identifier
            copied["activity_types_json"] = json.dumps(activity_types)
            copied["status"] = "CONNECTED" if connection_can_be_activated(copied) else "NEEDS_CONFIGURATION"
            imported.append(copied)
    except ValueError as error:
        return RedirectResponse("/settings?" + urlencode({"notice": f"Import failed: {error}"}), status_code=303)

    user = initial_user()
    created = 0
    updated = 0
    with connection() as db:
        for record in imported:
            existing = db.execute(
                "SELECT id FROM provider_connections WHERE user_id=? AND identifier=?",
                (user["id"], record["identifier"]),
            ).fetchone()
            if existing:
                db.execute(
                    """UPDATE provider_connections SET provider_type=?, display_name=?, endpoint_url=?, access_token=?,
                       external_account_id=?, oauth_client_id=?, oauth_client_secret=?, refresh_token=?, token_expires_at=?,
                       activity_types_json=?, status=?, oauth_state=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        record["provider_type"], record["display_name"], record["endpoint_url"], record["access_token"],
                        record["external_account_id"], record["oauth_client_id"], record["oauth_client_secret"],
                        record["refresh_token"], record["token_expires_at"], record["activity_types_json"],
                        record["status"], existing["id"],
                    ),
                )
                updated += 1
            else:
                db.execute(
                    """INSERT INTO provider_connections
                       (user_id, provider_type, display_name, identifier, endpoint_url, access_token, external_account_id,
                        oauth_client_id, oauth_client_secret, refresh_token, token_expires_at, activity_types_json, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (user["id"], *(record[field] for field in PROVIDER_TRANSFER_FIELDS),
                     record["activity_types_json"], record["status"]),
                )
                created += 1
    return RedirectResponse(
        "/settings?" + urlencode({"notice": f"Imported {created} connection(s); updated {updated}."}),
        status_code=303,
    )


BIKE_ARCHIVE_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
BIKE_COLOUR_DEFAULT = "#2f765b"


def bike_colour(value: object) -> str:
    """Accept a CSS hex colour while keeping chart data safe and predictable."""
    candidate = str(value or BIKE_COLOUR_DEFAULT).strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", candidate):
        raise ValueError("Choose a valid bike colour.")
    return candidate


def bike_archive_photo(filename: str) -> dict[str, str] | None:
    """Embed a stored bike image in a portable JSON archive."""
    candidate = (PHOTO_ROOT / filename).resolve()
    if candidate.parent != PHOTO_ROOT.resolve() or not candidate.is_file():
        return None
    mime_type = next((mime for mime, suffix in BIKE_ARCHIVE_IMAGE_TYPES.items() if candidate.suffix.lower() == suffix), None)
    if mime_type is None:
        return None
    content = candidate.read_bytes()
    if len(content) > 5 * 1024 * 1024:
        return None
    return {"mime_type": mime_type, "data_base64": base64.b64encode(content).decode("ascii")}


@app.post("/settings/bikes/export")
def export_bikes():
    """Download bike setup data and photos as one self-contained JSON archive."""
    user = initial_user()
    with connection() as db:
        bikes = db.execute("SELECT * FROM bikes WHERE user_id=? ORDER BY id", (user["id"],)).fetchall()
        if not bikes:
            return RedirectResponse("/settings?notice=No+bikes+to+export", status_code=303)
        records = []
        for bike in bikes:
            components = {
                row["component_type"]: row["ant_device_number"]
                for row in db.execute("SELECT component_type,ant_device_number FROM bike_components WHERE bike_id=?", (bike["id"],)).fetchall()
            }
            strava_gears = [dict(row) for row in db.execute(
                """SELECT pc.identifier AS connection_identifier, sg.external_gear_id
                   FROM bike_strava_gear_mappings mapping
                   JOIN strava_gears sg ON sg.id=mapping.strava_gear_id
                   JOIN provider_connections pc ON pc.id=sg.provider_connection_id
                   WHERE mapping.bike_id=? ORDER BY pc.identifier, sg.external_gear_id""",
                (bike["id"],),
            ).fetchall()]
            records.append({
                "name": bike["name"], "identifier": bike["identifier"], "bike_type": bike["bike_type"],
                "frame_number": bike["frame_number"], "details_markdown": bike["details_markdown"],
                "starting_mileage_m": bike["starting_mileage_m"], "colour": bike["colour"], "strava_activity_type": bike["strava_activity_type"],
                "components": components, "strava_gears": strava_gears, "photo": bike_archive_photo(bike["photo_filename"]),
            })
    export_document = {"format": "bike-garage-bikes", "version": 1, "exported_at": datetime.now(UTC).isoformat(), "bikes": records}
    filename = f"bike-garage-bikes-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
    return Response(json.dumps(export_document, indent=2) + "\n", media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


def parse_bike_archive(document: object) -> list[dict[str, object]]:
    if not isinstance(document, dict) or document.get("format") != "bike-garage-bikes" or document.get("version") != 1:
        raise ValueError("This is not a Bike Garage bike export.")
    records = document.get("bikes")
    if not isinstance(records, list) or not records or len(records) > 100:
        raise ValueError("The export must contain between 1 and 100 bikes.")
    parsed: list[dict[str, object]] = []
    seen_identifiers: set[str] = set()
    component_pairs: set[tuple[str, int]] = set()
    valid_component_types = {item[0] for item in BIKE_COMPONENT_TYPES}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Each bike must be an object.")
        name, identifier, bike_type = record.get("name"), record.get("identifier"), record.get("bike_type")
        if not isinstance(name, str) or not name.strip() or not isinstance(identifier, str) or not identifier.strip() or bike_type not in BIKE_TYPES:
            raise ValueError("Every bike needs a name, identifier, and valid type.")
        identifier = identifier.strip()
        if identifier in seen_identifiers:
            raise ValueError("The export contains the same bike identifier more than once.")
        seen_identifiers.add(identifier)
        frame, details = bike_configuration_fields(record.get("frame_number"), record.get("details_markdown"))
        try:
            mileage = max(0, float(record.get("starting_mileage_m") or 0))
            colour = bike_colour(record.get("colour"))
        except (TypeError, ValueError) as error:
            raise ValueError("A bike has an invalid starting mileage or colour.") from error
        activity_type = record.get("strava_activity_type")
        if activity_type is not None and activity_type not in BIKE_STRAVA_ACTIVITY_TYPE_VALUES:
            raise ValueError("A bike has an invalid Strava activity type.")
        raw_components = record.get("components") or {}
        if not isinstance(raw_components, dict):
            raise ValueError("A bike has invalid components.")
        components: dict[str, int] = {}
        for component_type, value in raw_components.items():
            if component_type not in valid_component_types or not isinstance(value, int) or not 1 <= value <= 65535:
                raise ValueError("A bike has an invalid component ANT+ ID.")
            pair = (component_type, value)
            if pair in component_pairs:
                raise ValueError("The export assigns one component ANT+ ID to more than one bike.")
            component_pairs.add(pair); components[component_type] = value
        photo = record.get("photo")
        photo_bytes: bytes | None = None; photo_suffix: str | None = None
        if photo is not None:
            if not isinstance(photo, dict) or photo.get("mime_type") not in BIKE_ARCHIVE_IMAGE_TYPES or not isinstance(photo.get("data_base64"), str):
                raise ValueError("A bike has an invalid photo.")
            try:
                photo_bytes = base64.b64decode(photo["data_base64"], validate=True)
            except (binascii.Error, ValueError) as error:
                raise ValueError("A bike photo is not valid Base64.") from error
            if not photo_bytes or len(photo_bytes) > 5 * 1024 * 1024:
                raise ValueError("A bike photo must be between 1 byte and 5 MB.")
            photo_suffix = BIKE_ARCHIVE_IMAGE_TYPES[photo["mime_type"]]
        gears = record.get("strava_gears") or []
        if not isinstance(gears, list) or any(not isinstance(item, dict) or not isinstance(item.get("connection_identifier"), str) or not isinstance(item.get("external_gear_id"), str) for item in gears):
            raise ValueError("A bike has invalid Strava bike mappings.")
        parsed.append({"name": name.strip(), "identifier": identifier, "bike_type": bike_type, "frame_number": frame, "details_markdown": details, "starting_mileage_m": mileage, "colour": colour, "strava_activity_type": activity_type, "components": components, "photo_bytes": photo_bytes, "photo_suffix": photo_suffix, "strava_gears": gears})
    return parsed


@app.post("/settings/bikes/import")
async def import_bikes(archive: UploadFile = File(...)):
    try:
        raw_document = await archive.read()
        if len(raw_document) > 35_000_000:
            raise ValueError("The file is too large.")
        records = parse_bike_archive(json.loads(raw_document.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return RedirectResponse("/settings?" + urlencode({"notice": f"Bike import failed: {error}"}), status_code=303)
    user = initial_user(); created = 0; updated = 0
    with connection() as db:
        for record in records:
            existing = db.execute("SELECT id,photo_filename FROM bikes WHERE user_id=? AND identifier=?", (user["id"], record["identifier"])).fetchone()
            bike_id = existing["id"] if existing else None
            ensure_component_ids_available(db, record["components"], exclude_bike_id=bike_id)
            photo_filename = existing["photo_filename"] if existing else placeholder_photo_filename()
            if record["photo_bytes"] is not None:
                photo_filename = f"{secrets.token_hex(16)}{record['photo_suffix']}"
                (PHOTO_ROOT / photo_filename).write_bytes(record["photo_bytes"])
            values = (record["name"], record["bike_type"], record["frame_number"], record["details_markdown"], photo_filename, record["starting_mileage_m"], record["colour"], record["strava_activity_type"])
            if existing:
                db.execute("UPDATE bikes SET name=?,bike_type=?,frame_number=?,details_markdown=?,photo_filename=?,starting_mileage_m=?,colour=?,strava_activity_type=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (*values, bike_id)); updated += 1
            else:
                bike_id = db.execute("INSERT INTO bikes (user_id,name,identifier,bike_type,owner_name,frame_number,details_markdown,photo_filename,starting_mileage_m,colour,strava_activity_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (user["id"], record["name"], record["identifier"], record["bike_type"], user["display_name"], record["frame_number"], record["details_markdown"], photo_filename, record["starting_mileage_m"], record["colour"], record["strava_activity_type"])).lastrowid; created += 1
            save_bike_components(db, bike_id, record["components"])
            db.execute("DELETE FROM bike_strava_gear_mappings WHERE bike_id=?", (bike_id,))
            for gear in record["strava_gears"]:
                match = db.execute("SELECT sg.id FROM strava_gears sg JOIN provider_connections pc ON pc.id=sg.provider_connection_id WHERE pc.identifier=? AND sg.external_gear_id=?", (gear["connection_identifier"], gear["external_gear_id"])).fetchone()
                if match:
                    db.execute("INSERT OR IGNORE INTO bike_strava_gear_mappings (bike_id,strava_gear_id) VALUES (?,?)", (bike_id, match["id"]))
    return RedirectResponse("/settings?" + urlencode({"notice": f"Imported {created} bike(s); updated {updated}."}), status_code=303)


RULE_ARCHIVE_FIELDS = (
    "name", "priority", "sport_type", "provider_account_id", "bike_count", "expression",
    "condition_operator", "condition_value", "is_catch_all", "enabled",
)


@app.post("/settings/rules/export")
def export_rules():
    user = initial_user()
    with connection() as db:
        rules = db.execute(
            "SELECT name,priority,sport_type,provider_account_id,bike_count,expression,condition_operator,condition_value,is_catch_all,enabled FROM resolver_rules WHERE user_id=? ORDER BY priority,id",
            (user["id"],),
        ).fetchall()
    if not rules:
        return RedirectResponse("/settings?notice=No+rules+to+export", status_code=303)
    document = {"format": "bike-garage-rules", "version": 1, "exported_at": datetime.now(UTC).isoformat(), "rules": [dict(rule) for rule in rules]}
    filename = f"bike-garage-rules-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
    return Response(json.dumps(document, indent=2) + "\n", media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


def parse_rule_archive(document: object) -> list[dict[str, object]]:
    if not isinstance(document, dict) or document.get("format") != "bike-garage-rules" or document.get("version") != 1:
        raise ValueError("This is not a Bike Garage rule export.")
    records = document.get("rules")
    if not isinstance(records, list) or not records or len(records) > 200:
        raise ValueError("The export must contain between 1 and 200 rules.")
    parsed: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Each rule must be an object.")
        name, expression = record.get("name"), record.get("expression")
        if not isinstance(name, str) or not name.strip() or not isinstance(expression, str) or not validate_expression(expression):
            raise ValueError("Every rule needs a name and a valid expression.")
        try:
            priority, bike_count, condition_value = int(record.get("priority", 100)), int(record.get("bike_count", 1)), int(record.get("condition_value", 1))
        except (TypeError, ValueError) as error:
            raise ValueError("A rule has invalid numeric values.") from error
        operator = record.get("condition_operator", ">=")
        if not 1 <= bike_count <= 6 or operator not in {">", ">=", "=", "<", "<="}:
            raise ValueError("A rule has invalid settings.")
        sport_type, account = record.get("sport_type"), record.get("provider_account_id")
        if sport_type is not None and not isinstance(sport_type, str) or account is not None and not isinstance(account, str):
            raise ValueError("A rule has an invalid filter.")
        parsed.append({"name": name.strip(), "priority": priority, "sport_type": sport_type, "provider_account_id": account, "bike_count": bike_count, "expression": expression, "condition_operator": operator, "condition_value": condition_value, "is_catch_all": int(bool(record.get("is_catch_all"))), "enabled": int(bool(record.get("enabled", True)))})
    return parsed


@app.post("/settings/rules/import")
async def import_rules(archive: UploadFile = File(...)):
    try:
        raw_document = await archive.read()
        if len(raw_document) > 2_000_000:
            raise ValueError("The file is too large.")
        records = parse_rule_archive(json.loads(raw_document.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return RedirectResponse("/settings?" + urlencode({"notice": f"Rule import failed: {error}"}), status_code=303)
    user = initial_user(); created = 0; updated = 0
    with connection() as db:
        for record in records:
            existing = db.execute("SELECT id FROM resolver_rules WHERE user_id=? AND name=? ORDER BY id LIMIT 1", (user["id"], record["name"])).fetchone()
            values = tuple(record[field] for field in RULE_ARCHIVE_FIELDS if field != "name")
            if existing:
                db.execute("UPDATE resolver_rules SET priority=?,sport_type=?,provider_account_id=?,bike_count=?,expression=?,condition_operator=?,condition_value=?,is_catch_all=?,enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (*values, existing["id"])); updated += 1
            else:
                db.execute("INSERT INTO resolver_rules (user_id,name,priority,sport_type,provider_account_id,bike_count,expression,condition_operator,condition_value,is_catch_all,enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (user["id"], *(record[field] for field in RULE_ARCHIVE_FIELDS))); created += 1
    return RedirectResponse("/settings?" + urlencode({"notice": f"Imported {created} rule(s); updated {updated}."}), status_code=303)


@app.post("/settings/export")
def export_settings_archive(sections: list[str] = Form(...)):
    """Create one portable archive containing the selected Garage entities."""
    allowed = {
        "providers": (export_provider_connections, "connections"),
        "bikes": (export_bikes, "bikes"),
        "rules": (export_rules, "rules"),
    }
    selected = list(dict.fromkeys(sections))
    if not selected or any(section not in allowed for section in selected):
        return RedirectResponse("/settings?notice=Choose+at+least+one+valid+export+area", status_code=303)
    payload: dict[str, object] = {}
    for section in selected:
        response = allowed[section][0]()
        # An empty area deliberately stays out of the combined archive; this
        # makes an all-selected export usable even for a new Garage.
        if isinstance(response, RedirectResponse):
            continue
        try:
            document = json.loads(response.body)
            payload[section] = document[allowed[section][1]]
        except (AttributeError, KeyError, TypeError, json.JSONDecodeError):
            return RedirectResponse("/settings?notice=Export+could+not+be+created", status_code=303)
    document = {
        "format": "bike-garage-settings-archive",
        "version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "sections": payload,
    }
    filename = f"bike-garage-export-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        json.dumps(document, indent=2) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


def import_error_from_response(response: RedirectResponse) -> str | None:
    """Convert a reused single-area import redirect into a combined error."""
    location = response.headers.get("location", "")
    values = parse_qs(location.partition("?")[2]).get("notice", [])
    notice = values[0] if values else ""
    return notice if "failed" in notice.lower() else None


@app.post("/settings/import")
async def import_settings_archive(archive: UploadFile = File(...)):
    """Import a combined Settings archive through the established validators."""
    try:
        raw_document = await archive.read()
        if len(raw_document) > 35_000_000:
            raise ValueError("The file is too large.")
        document = json.loads(raw_document.decode("utf-8"))
        sections = document.get("sections") if isinstance(document, dict) else None
        if not isinstance(document, dict) or document.get("format") != "bike-garage-settings-archive" or document.get("version") != 1:
            raise ValueError("This is not a Bike Garage combined export.")
        if not isinstance(sections, dict) or not sections:
            raise ValueError("The export does not contain any selected areas.")
        unknown = set(sections) - {"providers", "bikes", "rules"}
        if unknown:
            raise ValueError("The export contains an unsupported area.")
        if any(not isinstance(records, list) for records in sections.values()):
            raise ValueError("An export area is invalid.")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return RedirectResponse("/settings?" + urlencode({"notice": f"Import failed: {error}"}), status_code=303)

    handlers = {
        "providers": ("bike-garage-provider-connections", "connections", import_provider_connections),
        "bikes": ("bike-garage-bikes", "bikes", import_bikes),
        "rules": ("bike-garage-rules", "rules", import_rules),
    }
    imported_areas: list[str] = []
    for area, records in sections.items():
        if not records:
            continue
        archive_format, record_key, handler = handlers[area]
        nested_document = {"format": archive_format, "version": 1, record_key: records}
        nested_upload = UploadFile(filename=f"{area}.json", file=BytesIO(json.dumps(nested_document).encode("utf-8")))
        result = await handler(nested_upload)
        error = import_error_from_response(result)
        if error:
            return RedirectResponse("/settings?" + urlencode({"notice": error}), status_code=303)
        imported_areas.append(area)
    if not imported_areas:
        return RedirectResponse("/settings?notice=The+selected+export+areas+were+empty", status_code=303)
    return RedirectResponse("/settings?" + urlencode({"notice": f"Imported {', '.join(imported_areas)}."}), status_code=303)


@app.post("/connections/{connection_id}/activate")
def activate_provider_connection(connection_id: int):
    user = initial_user()
    with connection() as db:
        provider_connection = db.execute(
            "SELECT * FROM provider_connections WHERE id=? AND user_id=?", (connection_id, user["id"])
        ).fetchone()
        if provider_connection is None:
            return RedirectResponse("/providers?notice=Provider+connection+not+found", status_code=303)
        if not connection_can_be_activated(provider_connection):
            return RedirectResponse("/providers?notice=Connection+needs+configuration+before+it+can+be+activated", status_code=303)
        db.execute(
            "UPDATE provider_connections SET status='CONNECTED', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (connection_id,),
        )
    return RedirectResponse("/providers?notice=Provider+connection+activated", status_code=303)


@app.post("/connections/{connection_id}/deactivate")
def deactivate_provider_connection(connection_id: int):
    user = initial_user()
    with connection() as db:
        if db.execute(
            "SELECT 1 FROM provider_connections WHERE id=? AND user_id=?", (connection_id, user["id"])
        ).fetchone() is None:
            return RedirectResponse("/providers?notice=Provider+connection+not+found", status_code=303)
        db.execute(
            "UPDATE provider_connections SET status='DISCONNECTED', oauth_state=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (connection_id,),
        )
    return RedirectResponse("/providers?notice=Provider+connection+deactivated", status_code=303)


@app.post("/providers/activate-all")
def activate_all_provider_connections():
    user = initial_user()
    activated = 0
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM provider_connections WHERE user_id=? AND status='DISCONNECTED'", (user["id"],)
        ).fetchall()
        for row in rows:
            if connection_can_be_activated(row):
                db.execute("UPDATE provider_connections SET status='CONNECTED', updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
                activated += 1
    return RedirectResponse("/providers?" + urlencode({"notice": f"Activated {activated} provider connection(s)"}), status_code=303)


@app.post("/providers/deactivate-all")
def deactivate_all_provider_connections():
    user = initial_user()
    with connection() as db:
        deactivated = db.execute(
            """UPDATE provider_connections SET status='DISCONNECTED', oauth_state=NULL, updated_at=CURRENT_TIMESTAMP
               WHERE user_id=? AND status <> 'DISCONNECTED'""",
            (user["id"],),
        ).rowcount
    return RedirectResponse("/providers?" + urlencode({"notice": f"Deactivated {deactivated} provider connection(s)"}), status_code=303)


@app.get("/settings")
def settings_page(request: Request):
    with connection() as db:
        passkey = db.execute(
            "SELECT created_at,last_used_at FROM passkeys WHERE user_id=?", (initial_user()["id"],)
        ).fetchone()
        strava_auto_update = db.execute(
            "SELECT strava_auto_update_enabled FROM user_settings WHERE user_id=?", (initial_user()["id"],)
        ).fetchone()
        strava_auto_update_enabled = bool(strava_auto_update["strava_auto_update_enabled"]) if strava_auto_update else False
    scheduler_enabled, scheduler_interval_seconds = scheduler_configuration()
    return templates.TemplateResponse(
        request,
        "settings.html",
        page_context(
            request,
            passkey=dict(passkey) if passkey else None,
            scheduler_enabled=scheduler_enabled,
            scheduler_interval_minutes=scheduler_interval_seconds // 60,
            strava_auto_update_enabled=strava_auto_update_enabled,
        ),
    )


@app.post("/settings/scheduler")
async def update_scheduler_settings(request: Request):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    enabled = 1 if form.get("enabled", [""])[0] == "1" else 0
    try:
        interval_minutes = int(form.get("interval_minutes", [""])[0])
        if not 1 <= interval_minutes <= 1_440:
            raise ValueError
    except ValueError:
        return RedirectResponse(
            "/settings?notice=Choose+an+interval+between+1+and+1440+minutes", status_code=303
        )
    user = initial_user()
    with connection() as db:
        db.execute(
            """INSERT INTO user_settings (user_id,scheduler_enabled,scheduler_interval_seconds,updated_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET scheduler_enabled=excluded.scheduler_enabled,
                 scheduler_interval_seconds=excluded.scheduler_interval_seconds, updated_at=CURRENT_TIMESTAMP""",
            (user["id"], enabled, interval_minutes * 60),
        )
    notify_scheduler_settings_changed()
    state = "enabled" if enabled else "disabled"
    return RedirectResponse(
        "/settings?" + urlencode({"notice": f"Scheduler {state}; interval set to {interval_minutes} minutes"}),
        status_code=303,
    )


@app.post("/settings/strava-updates")
async def update_strava_auto_update_settings(request: Request):
    form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    enabled = 1 if form.get("enabled", [""])[0] == "1" else 0
    user = initial_user()
    with connection() as db:
        db.execute(
            """INSERT INTO user_settings (user_id,strava_auto_update_enabled,updated_at)
               VALUES (?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET strava_auto_update_enabled=excluded.strava_auto_update_enabled,
                 updated_at=CURRENT_TIMESTAMP""",
            (user["id"], enabled),
        )
    state = "enabled" if enabled else "disabled"
    return RedirectResponse(
        "/settings?" + urlencode({"notice": f"Automatic Strava activity updates {state}"}), status_code=303,
    )


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
    with connection() as db:
        deleted_activity_count = db.execute(
            "SELECT COUNT(*) FROM activities WHERE deleted_at IS NOT NULL"
        ).fetchone()[0]
    return templates.TemplateResponse(
        request,
        "activities.html",
        page_context(
            request,
            activity_filters=filters,
            deleted_activity_count=deleted_activity_count,
        ),
    )


@app.get("/activities/deleted")
def deleted_activities_page(request: Request):
    """Show soft-deleted activities separately, without mixing them into the stream."""
    with connection() as db:
        activities = [dict(row) for row in db.execute(
            """
            SELECT a.*, GROUP_CONCAT(pc.display_name, ', ') AS sources
            FROM activities a
            LEFT JOIN activity_provider_links apl ON apl.activity_id = a.id
            LEFT JOIN provider_activities pa ON pa.id = apl.provider_activity_id
            LEFT JOIN provider_connections pc ON pc.id = pa.connection_id
            WHERE a.deleted_at IS NOT NULL
            GROUP BY a.id
            ORDER BY a.deleted_at DESC, a.id DESC
            """
        ).fetchall()]
    return templates.TemplateResponse(
        request,
        "deleted_activities.html",
        page_context(request, deleted_activities=activities),
    )


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
        provider_snapshot = [
            {
                "id": int(row["provider_activity_id"]),
                "connection": str(row["connection"] or "Unknown provider"),
                "provider": str(row["provider"] or ""),
                "title": str(row["title"] or "Unnamed activity"),
                "started_at": str(row["started_at"] or ""),
            }
            for row in rows
        ]
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
                   (activity_id,resolution_id,rule_id,rule_name,result_text,log_output_json,provider_snapshot_json,applied)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (activity_id, f"manual-{secrets.token_hex(8)}", rule["id"], rule["name"], result_text, json.dumps(logs), json.dumps(provider_snapshot), 1),
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
            """SELECT resolution_id, rule_name, result_text, log_output_json, provider_snapshot_json, applied, created_at, id
               FROM activity_rule_runs WHERE activity_id=? ORDER BY id DESC LIMIT 180""",
            (activity_id,),
        ).fetchall()
        ui_log_rows = db.execute(
            "SELECT logger,message,created_at,id FROM activity_log_entries WHERE activity_id=? ORDER BY id DESC LIMIT 180",
            (activity_id,),
        ).fetchall()
        strava_mismatches = {
            item["provider_activity_id"]: item
            for item in strava_activity_mismatches(db, activity_id)
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
    # A resolver run records the exact provider activities linked at that
    # moment. Comparing resolver executions chronologically shows whether a
    # fresh source arrived or existing provider data changed afterwards.
    snapshots_by_resolution: dict[str, tuple[list[dict[str, object]], list[dict[str, object]], bool]] = {}
    previous_provider_ids: set[int] | None = None
    for row in reversed(run_rows):
        resolution_id = str(row["resolution_id"])
        if resolution_id in snapshots_by_resolution:
            continue
        try:
            snapshot = json.loads(row["provider_snapshot_json"] or "[]")
        except json.JSONDecodeError:
            snapshot = []
        snapshot = [item for item in snapshot if isinstance(item, dict)] if isinstance(snapshot, list) else []
        provider_ids = {
            int(item["id"])
            for item in snapshot
            if str(item.get("id", "")).isdigit()
        }
        added = (
            snapshot
            if previous_provider_ids is None
            else [item for item in snapshot if str(item.get("id", "")).isdigit() and int(item["id"]) not in previous_provider_ids]
        )
        snapshots_by_resolution[resolution_id] = (snapshot, added, previous_provider_ids is not None)
        if snapshot:
            previous_provider_ids = provider_ids

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
        snapshot, added, has_previous_snapshot = snapshots_by_resolution.get(
            str(item["resolution_id"]), ([], [], False)
        )
        added_ids = {
            int(source["id"])
            for source in added
            if str(source.get("id", "")).isdigit()
        }
        log_providers = [
            {
                "provider": str(source.get("provider") or ""),
                "connection": str(source.get("connection") or source.get("provider") or "Unknown provider"),
                "title": str(source.get("title") or "Unnamed activity"),
                "is_new": int(source["id"]) in added_ids if str(source.get("id", "")).isdigit() else False,
            }
            for source in snapshot
        ]
        activity_logs.append({
            "created_at": item["created_at"],
            "logger": f"rule_{item['rule_name']}",
            "message": "\n".join(str(message) for message in messages),
            "provider_activities": log_providers,
            "provider_snapshot_available": bool(snapshot),
            "provider_snapshot_changed": bool(added) if has_previous_snapshot else False,
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
        item["strava_mismatch"] = strava_mismatches.get(item["id"])
        try:
            payload = json.loads(item["raw_json"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        item["device_name"] = payload.get("device_name") or ""
        item["gear_id"] = payload.get("gear_id") or ""
        item["moving_time"] = payload.get("moving_time")
        raw_duration = provider_duration(payload, item["provider_type"])
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
                    # This is a detail refresh, not a new provider import.
                    # Keep imported_at immutable so the activity timeline
                    # continues to show when the source first arrived.
                    db.execute("UPDATE provider_activities SET raw_json=? WHERE id=?", (json.dumps(enriched), item["id"]))
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
    strava_activity_url = next((
        f"https://www.strava.com/activities/{item['external_activity_id']}"
        for item in providers
        if item["provider_type"] in {"STRAVA", "STRAVA_PROXY"} and item.get("external_activity_id")
    ), None)
    return templates.TemplateResponse(
        request,
        "activity_detail.html",
        page_context(request, activity=activity, provider_activities=providers, route_polyline=route_polyline, bike_assignments=assignments, resolver_runs=resolver_runs, activity_logs=activity_logs, strava_activity_url=strava_activity_url),
    )


@app.post("/activities/{activity_uuid}/strava/{provider_activity_id}/update")
def update_activity_strava(activity_uuid: str, provider_activity_id: int):
    """Push configured Bike Garage bike fields to one matching Strava source."""
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
        mismatch = next(
            (
                item
                for item in strava_activity_mismatches(db, activity["id"])
                if item["provider_activity_id"] == provider_activity_id
            ),
            None,
        )
        updated, skipped, failed = sync_strava_activity(db, activity["id"], provider_activity_id)
    if failed:
        notice = "Strava update failed; check the activity log for details"
    elif updated and mismatch:
        if mismatch["gear_mismatch"] and mismatch["activity_type_mismatch"]:
            notice = "Strava bike and activity type updated"
        elif mismatch["gear_mismatch"]:
            notice = "Strava bike updated"
        else:
            notice = "Strava activity type updated"
    elif skipped:
        notice = "Strava was not updated: activity is before Garage start"
    else:
        notice = "Strava is already up to date or no matching Bike Garage mapping is configured"
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
    raw_duration = provider_duration(payload, source["provider_type"])
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


@app.get("/provider-activities/{provider_activity_id}/fit")
def download_hammerhead_fit(provider_activity_id: int):
    """Download a Hammerhead FIT file without exposing the connection token."""
    with connection() as db:
        source = db.execute(
            """SELECT pa.external_activity_id, pa.name, pc.*
               FROM provider_activities pa
               JOIN provider_connections pc ON pc.id=pa.connection_id
               LEFT JOIN activity_provider_links apl ON apl.provider_activity_id=pa.id
               LEFT JOIN activities a ON a.id=apl.activity_id
               WHERE pa.id=? AND pc.provider_type='HAMMERHEAD' AND a.deleted_at IS NULL""",
            (provider_activity_id,),
        ).fetchone()
        if source is None:
            return RedirectResponse("/activities?notice=Hammerhead+provider+activity+not+found", status_code=303)
        try:
            active_connection = refresh_hammerhead_connection_token(db, source)
            fit_bytes = fetch_hammerhead_activity_fit(
                active_connection["endpoint_url"], active_connection["access_token"], source["external_activity_id"],
            )
        except Exception as error:
            return RedirectResponse(
                f"/provider-activities/{provider_activity_id}?" + urlencode({"notice": f"FIT download failed: {error}"}),
                status_code=303,
            )
    filename = f"hammerhead-activity-{source['external_activity_id']}.fit"
    return Response(
        fit_bytes,
        media_type="application/vnd.ant.fit",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/provider-activities/{provider_activity_id}/refresh")
def refresh_strava_provider_activity(provider_activity_id: int):
    """Explicitly refresh one Strava source without running the full importer."""
    with connection() as db:
        source = db.execute(
            """SELECT pa.id, pa.external_activity_id, pc.endpoint_url, pc.access_token, pc.external_account_id
               FROM provider_activities pa JOIN provider_connections pc ON pc.id=pa.connection_id
               WHERE pa.id=? AND pc.provider_type='STRAVA_PROXY'""",
            (provider_activity_id,),
        ).fetchone()
        if source is None:
            return RedirectResponse("/activities?notice=Strava+provider+activity+not+found", status_code=303)
        try:
            payload = fetch_strava_activity(
                source["endpoint_url"] or "", source["access_token"], source["external_account_id"] or "",
                source["external_activity_id"],
            )
        except Exception as error:
            return RedirectResponse(
                f"/provider-activities/{provider_activity_id}?" + urlencode({"notice": f"Strava refresh failed: {error}"}),
                status_code=303,
            )
        db.execute(
            """UPDATE provider_activities SET name=?, sport_type=?, distance_m=?, raw_json=? WHERE id=?""",
            (
                payload.get("name") or "Unnamed activity", payload.get("sport_type") or payload.get("type"),
                float(payload.get("distance") or 0), json.dumps(payload), provider_activity_id,
            ),
        )
    return RedirectResponse(
        f"/provider-activities/{provider_activity_id}?notice=Strava+activity+details+refreshed", status_code=303
    )


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
        provider_snapshot = [
            {
                "id": int(row["provider_activity_id"]),
                "connection": str(row["connection"] or "Unknown provider"),
                "provider": str(row["provider"] or ""),
                "title": str(row["title"] or "Unnamed activity"),
                "started_at": str(row["started_at"] or ""),
            }
            for row in provider_rows
        ]
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
               (activity_id,resolution_id,rule_id,rule_name,result_text,log_output_json,provider_snapshot_json,applied)
               VALUES (?,?,?,?,?,?,?,?)""",
            (activity_id, f"manual-{secrets.token_hex(8)}", rule["id"], rule["name"], result_text, json.dumps(logs), json.dumps(provider_snapshot), int(applied)),
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
    strava_failed = 0
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
            strava_updated, strava_skipped, strava_failed = sync_strava_activity(db, activity_id)
    notice = "Bike assignment updated"
    if strava_updated:
        notice += f"; Strava updated for {strava_updated} activit{'ies' if strava_updated != 1 else 'y'}"
    elif strava_skipped:
        notice += "; Strava was not updated before Garage start"
    if strava_failed:
        notice += f"; Strava update failed for {strava_failed} activit{'ies' if strava_failed != 1 else 'y'}"
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {"message": notice, "strava_updated": strava_updated, "strava_failed": strava_failed},
            status_code=502 if strava_failed else 200,
        )
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


@app.post("/activities/{activity_uuid}/restore")
def restore_activity(activity_uuid: str):
    """Restore a soft-deleted activity and preserve its audit history."""
    with connection() as db:
        activity = db.execute(
            "SELECT id FROM activities WHERE public_id=? AND deleted_at IS NOT NULL",
            (activity_uuid,),
        ).fetchone()
        if activity is None:
            return RedirectResponse("/activities/deleted?notice=Deleted+activity+not+found", status_code=303)
        db.execute(
            "UPDATE activities SET deleted_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (activity["id"],),
        )
        record_activity_log(db, activity["id"], "Activity restored from deleted activities.")
    return RedirectResponse("/activities/deleted?notice=Activity+restored", status_code=303)


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
        page_context(request, bike=None, components={}, strava_gears=gears, strava_gear_errors=gear_errors,
                     bike_strava_activity_types=BIKE_STRAVA_ACTIVITY_TYPES),
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


def bike_configuration_fields(frame_number: str | None, details_markdown: str | None) -> tuple[str, str]:
    """Validate the free-form bicycle configuration without limiting Markdown syntax."""
    frame = (frame_number or "").strip()
    details = (details_markdown or "").replace("\r\n", "\n")
    if len(frame) > 160:
        raise ValueError("Frame number must be 160 characters or fewer.")
    if len(details) > 100_000:
        raise ValueError("Bike details must be 100,000 characters or fewer.")
    return frame, details


@app.post("/bikes")
async def create_bike(
    name: str | None = Form(None), identifier: str | None = Form(None), bike_type: str | None = Form(None),
    frame_number: str | None = Form(None), details_markdown: str | None = Form(None), photo: UploadFile | None = File(None),
    starting_mileage_km: str | None = Form(None),
    colour: str | None = Form(None),
    shifting_ant_device_number: str | None = Form(None), bike_power_ant_device_number: str | None = Form(None),
    seatpost_ant_device_number: str | None = Form(None),
    strava_gear_ids: list[str] = Form([]),
    strava_activity_type: str | None = Form(None),
):
    if not name or not identifier or not bike_type:
        return RedirectResponse(
            "/bikes/new?" + urlencode({"notice": "Name, identifier, and bike type are required."}),
            status_code=303,
        )
    if bike_type not in BIKE_TYPES:
        return RedirectResponse(
            "/bikes/new?" + urlencode({"notice": "Choose Road, Gravel, or Mountainbike as the bike type."}),
            status_code=303,
        )
    if strava_activity_type and strava_activity_type not in BIKE_STRAVA_ACTIVITY_TYPE_VALUES:
        return RedirectResponse(
            "/bikes/new?" + urlencode({"notice": "Choose a valid Strava activity type."}), status_code=303
        )
    try:
        photo_filename = save_photo(photo) if photo and photo.filename else placeholder_photo_filename()
        component_ant_ids = component_ant_ids_from_form(locals())
        starting_mileage_m = max(0, float(starting_mileage_km or "0") * 1000)
        cleaned_frame_number, cleaned_details_markdown = bike_configuration_fields(frame_number, details_markdown)
        cleaned_colour = bike_colour(colour)
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
            """INSERT INTO bikes
               (user_id, name, identifier, bike_type, owner_name, frame_number, details_markdown, photo_filename, starting_mileage_m, colour, strava_activity_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user["id"], name.strip(), identifier.strip(), bike_type.strip(), user["display_name"], cleaned_frame_number,
             cleaned_details_markdown, photo_filename, starting_mileage_m, cleaned_colour, strava_activity_type or None),
        )
        save_bike_components(db, cursor.lastrowid, component_ant_ids)
        save_bike_strava_gears(db, cursor.lastrowid, strava_gear_ids)
    return RedirectResponse("/bikes?notice=Bike+created", status_code=303)


@app.get("/bikes/{bike_id}")
def bike_detail(request: Request, bike_id: int):
    user = initial_user()
    bike_activities: list[dict[str, object]] = []
    excluded_bike_activity_count = 0
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
            activity_query = """
                SELECT a.id AS activity_id, a.public_id AS activity_public_id,
                       a.name, a.started_at, a.distance_m, a.sport_type,
                       aba.slot_index, aba.source
                FROM activity_bike_assignments aba
                JOIN activities a ON a.id = aba.activity_id
                WHERE aba.bike_id = ? AND a.deleted_at IS NULL
            """
            activity_params: tuple[object, ...] = (bike_id,)
            if tracking_start is not None:
                activity_query += " AND a.started_at_epoch >= ?"
                activity_params += (tracking_start,)
                excluded_bike_activity_count = int(db.execute(
                    """SELECT COUNT(DISTINCT a.id)
                       FROM activity_bike_assignments aba JOIN activities a ON a.id=aba.activity_id
                       WHERE aba.bike_id=? AND a.deleted_at IS NULL AND a.started_at_epoch<?""",
                    (bike_id, tracking_start),
                ).fetchone()[0] or 0)
            activity_query += " ORDER BY COALESCE(a.started_at_epoch, 0) DESC, a.id DESC, aba.slot_index ASC"
            bike_activities = [dict(row) for row in db.execute(activity_query, activity_params).fetchall()]
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
            bike_activities=bike_activities, excluded_bike_activity_count=excluded_bike_activity_count,
            strava_gear_links=strava_gear_links,
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
        page_context(request, bike=bike, components=components, strava_gears=gears, strava_gear_errors=gear_errors,
                     bike_strava_activity_types=BIKE_STRAVA_ACTIVITY_TYPES),
    )


@app.post("/bikes/{bike_id}/edit")
async def update_bike(
    bike_id: int, name: str | None = Form(None), identifier: str | None = Form(None), bike_type: str | None = Form(None),
    frame_number: str | None = Form(None), details_markdown: str | None = Form(None),
    starting_mileage_km: str | None = Form(None),
    colour: str | None = Form(None),
    photo: UploadFile | None = File(None),
    shifting_ant_device_number: str | None = Form(None), bike_power_ant_device_number: str | None = Form(None),
    seatpost_ant_device_number: str | None = Form(None),
    strava_gear_ids: list[str] = Form([]),
    strava_activity_type: str | None = Form(None),
):
    if not name or not identifier or not bike_type:
        return RedirectResponse(
            f"/bikes/{bike_id}/edit?" + urlencode({"notice": "Name, identifier, and bike type are required."}),
            status_code=303,
        )
    if bike_type not in BIKE_TYPES:
        return RedirectResponse(
            f"/bikes/{bike_id}/edit?" + urlencode({"notice": "Choose Road, Gravel, or Mountainbike as the bike type."}),
            status_code=303,
        )
    if strava_activity_type and strava_activity_type not in BIKE_STRAVA_ACTIVITY_TYPE_VALUES:
        return RedirectResponse(
            f"/bikes/{bike_id}/edit?" + urlencode({"notice": "Choose a valid Strava activity type."}),
            status_code=303,
        )
    try:
        starting_mileage_m = max(0, float(starting_mileage_km or "0") * 1000)
        component_ant_ids = component_ant_ids_from_form(locals())
        cleaned_frame_number, cleaned_details_markdown = bike_configuration_fields(frame_number, details_markdown)
        cleaned_colour = bike_colour(colour)
    except ValueError as error:
        return RedirectResponse(
            f"/bikes/{bike_id}/edit?" + urlencode({"notice": str(error)}),
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
            """UPDATE bikes SET name=?, identifier=?, bike_type=?, frame_number=?, details_markdown=?, photo_filename=?,
               starting_mileage_m=?, colour=?, strava_activity_type=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (name.strip(), identifier.strip(), bike_type.strip(), cleaned_frame_number, cleaned_details_markdown,
             photo_filename, starting_mileage_m, cleaned_colour, strava_activity_type or None, bike_id),
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


@app.get("/sync/progress")
def sync_progress(request: Request):
    """Stream manual-sync milestones so the Activities page stays responsive."""
    updates: Queue[tuple[str, object]] = Queue()
    structured = request.query_params.get("format") == "structured"

    def publish_progress(message: object) -> None:
        """Keep already-open pages on the legacy, readable text protocol."""
        if structured:
            updates.put(("progress", message))
            return
        if not isinstance(message, dict):
            updates.put(("progress", message))
            return
        if message.get("type") == "providers":
            return
        if message.get("type") == "provider":
            label = message.get("label", "Provider")
            updates.put(("progress", f"{label}: {message.get('status', 'Working…')}"))
            return
        if message.get("type") == "overall":
            updates.put(("progress", message.get("status", "Working on your activity data…")))

    def worker() -> None:
        errors: list[str] = []
        try:
            publish_progress({"type": "overall", "status": "Preparing provider synchronization…"})
            imported = sync_all_connections(errors, progress=publish_progress)
            if errors:
                notice = f"Sync finished with provider errors. Imported {imported} new provider activities."
            else:
                with connection() as db:
                    connected_count = db.execute(
                        "SELECT COUNT(*) FROM provider_connections WHERE provider_type IN ('STRAVA_PROXY', 'HAMMERHEAD') AND status = 'CONNECTED'"
                    ).fetchone()[0]
                notice = ("No active Strava Proxy or Hammerhead connection found."
                          if connected_count == 0 else f"Sync complete. Imported {imported} new provider activities.")
            updates.put(("complete", {"notice": notice, "errors": errors}))
        except Exception:
            updates.put(("failed", "Sync could not be completed. Please try again."))

    Thread(target=worker, name="bike-garage-manual-sync", daemon=True).start()

    def events():
        while True:
            event, payload = updates.get()
            yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"
            if event in {"complete", "failed"}:
                break

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        preserve_deactivated_state = provider_connection["status"] == "DISCONNECTED"
        if provider_connection["provider_type"] == "HAMMERHEAD":
            try:
                provider_connection = refresh_hammerhead_connection_token(db, provider_connection)
            except Exception as error:
                db.execute(
                    """UPDATE provider_connections
                       SET status=?, last_tested_at=CURRENT_TIMESTAMP,
                           last_test_status='FAILED', last_test_message=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    ("DISCONNECTED" if preserve_deactivated_state else "NEEDS_CONFIGURATION",
                     f"Hammerhead token refresh failed: {error}", connection_id),
                )
                return RedirectResponse(
                    "/providers?" + urlencode({"test_connection": connection_id}), status_code=303
                )
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
                "DISCONNECTED" if preserve_deactivated_state else ("CONNECTED" if result.is_healthy else "NEEDS_CONFIGURATION"),
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
    redirect_uri = public_origin(request) + "/providers/HAMMERHEAD/callback"
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
    redirect_uri = public_origin(request) + "/providers/HAMMERHEAD/callback"
    try:
        token = exchange_hammerhead_code(provider_connection["oauth_client_id"], provider_connection["oauth_client_secret"], code, redirect_uri)
    except Exception as exc:
        return RedirectResponse("/providers?" + urlencode({"notice": f"Hammerhead token exchange failed: {exc}"}), status_code=303)
    expires_at = int(time.time()) + max(60, int(token.get("expires_in") or 3600))
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
