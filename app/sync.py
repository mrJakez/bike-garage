from __future__ import annotations

import json
import ast
import re
import difflib
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from .connectors.strava_proxy import fetch_activities
from .connectors.hammerhead import (
    fetch_activities as fetch_hammerhead_activities,
    fetch_activity_detail as fetch_hammerhead_activity_detail,
    fetch_activity_fit as fetch_hammerhead_activity_fit,
    refresh_access_token as refresh_hammerhead_access_token,
)
from .fit_hardware import extract_device_info
from .database import connection


try:
    DEFAULT_SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
except ValueError:
    DEFAULT_SYNC_INTERVAL_SECONDS = 300
DEFAULT_SYNC_INTERVAL_SECONDS = max(60, min(86_400, DEFAULT_SYNC_INTERVAL_SECONDS))
_stop = threading.Event()
_scheduler_wake = threading.Event()


def scheduler_configuration() -> tuple[bool, int]:
    """Read the persistent scheduler setting, falling back to the deployment default."""
    with connection() as db:
        row = db.execute(
            "SELECT scheduler_enabled, scheduler_interval_seconds FROM user_settings ORDER BY user_id LIMIT 1"
        ).fetchone()
    if row is None:
        return True, DEFAULT_SYNC_INTERVAL_SECONDS
    enabled = bool(row["scheduler_enabled"])
    interval = row["scheduler_interval_seconds"] or DEFAULT_SYNC_INTERVAL_SECONDS
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = DEFAULT_SYNC_INTERVAL_SECONDS
    return enabled, max(60, min(86_400, interval))


def notify_scheduler_settings_changed() -> None:
    """Wake the background loop so a saved Settings change takes effect immediately."""
    _scheduler_wake.set()


def store_fit_hardware(db: Any, provider_activity_id: int, fit_bytes: bytes) -> list[dict[str, Any]]:
    """Replace normalized FIT device observations for one provider activity."""
    observations = extract_device_info(fit_bytes)
    db.execute("DELETE FROM provider_activity_hardware WHERE provider_activity_id=?", (provider_activity_id,))
    for item in observations:
        db.execute(
            """INSERT OR IGNORE INTO provider_activity_hardware
               (provider_activity_id,component_type,protocol,ant_device_number,manufacturer_id,manufacturer_name,
                device_type_id,serial_number,product_id,product_name,battery_status,raw_json,observed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (provider_activity_id, item["component_type"], item["protocol"], item["ant_device_number"],
             item["manufacturer_id"], item["manufacturer_name"], item["device_type_id"], item["serial_number"],
             item["product_id"], item["product_name"], item["battery_status"], json.dumps(item["raw"]), item["observed_at"]),
        )
    return observations


def fetch_missing_hammerhead_hardware(
    db: Any, connection_row: Any, provider_activity_id: int, external_activity_id: str,
) -> bool:
    """Persist FIT hardware before evaluating rules for a Hammerhead source.

    The activity-list payload has no component ANT+ IDs.  Resolver rules that
    select a bike from those IDs therefore must wait for the FIT download;
    otherwise a newly imported ride can only resolve on a later manual test.
    """
    already_present = db.execute(
        "SELECT 1 FROM provider_activity_hardware WHERE provider_activity_id=? LIMIT 1",
        (provider_activity_id,),
    ).fetchone()
    if already_present is not None:
        return True
    try:
        fit_bytes = fetch_hammerhead_activity_fit(
            connection_row["endpoint_url"], connection_row["access_token"], external_activity_id,
        )
        return bool(store_fit_hardware(db, provider_activity_id, fit_bytes))
    except (HTTPError, URLError, TimeoutError, ValueError):
        # Keep importing the activity even if Hammerhead has not made its FIT
        # file available yet. A future sync retries before running the rule.
        return False


def refresh_hammerhead_connection_token(
    db: Any, connection_row: Any, *, force: bool = False,
) -> dict[str, Any]:
    """Refresh an expiring Hammerhead token and return its persisted row.

    Hammerhead supplies a refresh token with OAuth authorization.  The old
    flow stored it but never redeemed it, so a routine access-token expiry
    incorrectly looked like a connection that had to be set up again.
    """
    connection = dict(connection_row)
    expires_at = int(connection.get("token_expires_at") or 0)
    if not force and connection.get("access_token") and expires_at > int(time.time()) + 60:
        return connection
    token = refresh_hammerhead_access_token(
        connection.get("oauth_client_id") or "",
        connection.get("oauth_client_secret") or "",
        connection.get("refresh_token") or "",
    )
    refreshed_expires_at = int(time.time()) + max(60, int(token.get("expires_in") or 3600))
    db.execute(
        """UPDATE provider_connections
           SET access_token=?, refresh_token=?, token_expires_at=?, status='CONNECTED', updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            token["access_token"], token.get("refresh_token") or connection.get("refresh_token"),
            refreshed_expires_at, connection["id"],
        ),
    )
    return dict(db.execute("SELECT * FROM provider_connections WHERE id=?", (connection["id"],)).fetchone())


def attach_hardware_observations(db: Any, rows: list[Any]) -> list[dict[str, Any]]:
    """Attach resolver-safe FIT hardware data to provider-activity rows."""
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        provider_activity_id = item.get("provider_activity_id") or item.get("id")
        hardware_rows = db.execute(
            "SELECT component_type,protocol,ant_device_number,manufacturer_id,manufacturer_name,device_type_id,serial_number,product_id,product_name,battery_status,raw_json FROM provider_activity_hardware WHERE provider_activity_id=? ORDER BY id",
            (provider_activity_id,),
        ).fetchall() if provider_activity_id else []
        item["hardware"] = [dict(hardware) for hardware in hardware_rows]
        output.append(item)
    return output

def strip_rule_comment(line: str) -> str:
    """Remove // comments while preserving // inside quoted strings."""
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote:
            escaped = True
        elif char in ("'", '"'):
            quote = None if quote == char else (char if quote is None else quote)
        elif char == "/" and not quote and index + 1 < len(line) and line[index + 1] == "/":
            return line[:index]
    return line

class ProviderActivities:
    def __init__(self, rows): self.rows = rows
    @property
    def count(self): return len(self.rows)
    @property
    def first(self): return ProviderActivity(self.rows[0] if self.rows else {})
    def filter(self, provider=None, account=None, connection=None):
        rows = [
            row for row in self.rows
            if (provider is None or row.get("provider") == provider)
            and (account is None or row.get("account") == account)
            and (connection is None or row.get("connection") == connection)
        ]
        # A Connection identifier is unique in the garage. Make this common
        # lookup ergonomic: no redundant `.first` is required after filtering
        # for one specific connection.
        if connection is not None:
            return ProviderActivity(rows[0] if rows else {})
        return ProviderActivities(rows)
    def find(self, provider=None, account=None, connection=None):
        """Return the one provider activity selected by a unique connection."""
        rows = [
            row for row in self.rows
            if (provider is None or row.get("provider") == provider)
            and (account is None or row.get("account") == account)
            and (connection is None or row.get("connection") == connection)
        ]
        return ProviderActivity(rows[0]) if rows else None


class RuleList(list):
    """A small rule-language list with collection-style properties.

    Python's built-in ``list.count`` is a function which requires an argument.
    Rules intentionally use ``.count`` as a property, just like
    ``activity.providerActivities.count``.
    """
    @property
    def count(self):
        return len(self)

class HardwareObservation:
    def __init__(self, row: dict[str, Any] | None = None): self.row = row or {}
    def __eq__(self, other): return other is None and not self.row
    def __ne__(self, other): return not self.__eq__(other)
    @property
    def antDeviceNumber(self): return self.row.get("ant_device_number")
    @property
    def componentType(self): return self.row.get("component_type") or ""
    @property
    def manufacturer(self): return self.row.get("manufacturer_name") or ""
    @property
    def serialNumber(self): return self.row.get("serial_number")
    @property
    def json(self): return str(self)
    def __str__(self): return "null" if not self.row else json.dumps(self.row, ensure_ascii=False, default=str, sort_keys=True)


class ProviderActivity:
    def __init__(self, row): self.row = row
    def __eq__(self, other):
        return other is None and not self.row
    def __ne__(self, other):
        return not self.__eq__(other)
    def __str__(self):
        """Make printing an object useful in the rule test log."""
        if not self.row:
            return "null"
        payload = self.row.get("raw_json")
        try:
            details = json.loads(payload) if payload else dict(self.row)
        except (TypeError, json.JSONDecodeError):
            details = dict(self.row)
        # Rule-facing source metadata belongs in the debug representation too,
        # even when it is not part of the provider's native JSON payload.
        if isinstance(details, dict):
            details.update({key: self.row[key] for key in ("provider", "connection", "account") if key in self.row})
        return json.dumps(details, ensure_ascii=False, default=str, sort_keys=True)
    @property
    def count(self):
        return 1 if self.row else 0
    @property
    def first(self):
        """Keeps older `filter(connection=...).first` rules compatible."""
        return self
    @property
    def json(self):
        return str(self)
    @property
    def title(self): return self.row.get("title") or self.row.get("name") or ""
    @property
    def provider(self): return self.row.get("provider") or ""
    @property
    def account(self): return self.row.get("account") or ""
    @property
    def connection(self): return self.row.get("connection") or ""
    @property
    def sportType(self): return self.row.get("sport_type") or ""
    @property
    def distance(self): return self.row.get("distance_m") or 0
    @property
    def startedAt(self): return self.row.get("started_at") or ""
    def hardware(self, component_type=None):
        """Return one FIT hardware observation by normalized component type."""
        hardware = self.row.get("hardware") or []
        match = next((item for item in hardware if item.get("component_type") == component_type), None)
        return HardwareObservation(match)

class Bike:
    def __init__(self, row): self.row = row or {}
    @property
    def id(self): return self.row.get("id")
    @property
    def identifier(self): return self.row.get("identifier") or ""
    @property
    def name(self): return self.row.get("name") or ""
    @property
    def json(self):
        return str(self)
    def hardware(self, component_type=None):
        """Return the configured bike component for a resolver expression.

        Bikes use the same small observation surface as provider FIT data, so
        rules can compare `bike.hardware("shifting").antDeviceNumber` with a
        Hammerhead activity's observed device number.
        """
        components = self.row.get("components") or []
        match = next((item for item in components if item.get("component_type") == component_type), None)
        return HardwareObservation(match)
    def __str__(self):
        """Expose the full object in rule-test print output."""
        return json.dumps(self.row, ensure_ascii=False, default=str, sort_keys=True)

class BikeCatalog:
    def __init__(self, rows): self.rows = [Bike(row) for row in rows]
    def find(self, identifier=None, antDeviceNumber=None, component=None):
        if identifier is not None:
            return next((bike for bike in self.rows if bike.identifier == identifier), None)
        # `providerActivity.hardware("shifting")` is a useful value to pass
        # straight into this lookup. Accept it as well as a raw ANT+ number.
        if isinstance(antDeviceNumber, HardwareObservation):
            if component is None:
                component = antDeviceNumber.componentType or None
            antDeviceNumber = antDeviceNumber.antDeviceNumber
        if antDeviceNumber is not None:
            return next((bike for bike in self.rows if any(
                item.get("ant_device_number") == antDeviceNumber
                and (component is None or item.get("component_type") == component)
                for item in (bike.row.get("components") or [])
            )), None)
        return None

class ActivityContext:
    def __init__(self, rows, activity=None, bike_rows=None, assigned_bikes=None):
        self.providerActivities = ProviderActivities(rows)
        self._activity = activity or {}
        self.bikeCount = int(self._activity.get("expected_bike_count") or 1)
        # A rule can deliberately assign an empty list to clear old RULE
        # assignments. Keep that distinct from a rule that never touches bikes.
        self._bikes = [Bike(row) for row in (assigned_bikes or [])]
        self.bikes_changed = False
        self.availableBikes = BikeCatalog(bike_rows or [])

    @property
    def bikes(self):
        return self._bikes

    @bikes.setter
    def bikes(self, value):
        self._bikes = value
        self.bikes_changed = True
    @property
    def title(self):
        return self._activity.get("title") or self._activity.get("name") or ""
    @title.setter
    def title(self, value):
        self._activity["name"] = str(value or "")
    @property
    def sportType(self):
        return self._activity.get("sport_type") or self._activity.get("sportType") or ""
    @property
    def distance(self):
        return self._activity.get("distance_m") or self._activity.get("distance") or 0
    @property
    def startedAt(self):
        return self._activity.get("started_at") or self._activity.get("startedAt") or ""


def epoch_from_activity(activity: dict[str, Any]) -> int | None:
    value = activity.get("start_date") or activity.get("start_date_local")
    if isinstance(value, (int, float)):
        return int(value)
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def display_started_at(activity: dict[str, Any], provider_type: str | None) -> str | None:
    """Return the timestamp that should be shown to a person.

    Strava provides two distinct fields: ``start_date`` is UTC and is used for
    ordering/matching, while ``start_date_local`` is already the ride's wall
    clock time.  Our proxy currently serialises the latter with a trailing
    ``Z`` as well.  Treating that suffix as UTC shifted the UI by two hours in
    Berlin.  Store the local Strava value without an offset, so the UI does
    not convert it a second time.
    """
    value = activity.get("start_date_local") or activity.get("start_date")
    if value is None:
        return None
    if provider_type != "STRAVA_PROXY":
        return str(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None).isoformat()
    except (TypeError, ValueError):
        return str(value).removesuffix("Z")


def provider_endpoint(source: object, endpoint: str) -> tuple[float, float] | None:
    """Read a provider activity's GPS endpoint from common provider payload shapes."""
    row = source.row if isinstance(source, ProviderActivity) else source if isinstance(source, dict) else {}
    if not row:
        return None
    try:
        payload = json.loads(row.get("raw_json") or "{}") if isinstance(row, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return None
    aliases = (f"{endpoint}_latlng", f"{endpoint}LatLng", f"{endpoint}_location")
    value = next((payload.get(key) for key in aliases if payload.get(key) is not None), None)
    if value is None:
        nested = payload.get("raw_strava") or payload.get("raw_hammerhead") or {}
        nested = nested if isinstance(nested, dict) else {}
        value = next((nested.get(key) for key in aliases if nested.get(key) is not None), None)
    try:
        if isinstance(value, dict):
            return float(value.get("lat") or value.get("latitude")), float(value.get("lng") or value.get("longitude"))
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    return None


def geo_distance_meters(point: tuple[float, float] | None, latitude: object, longitude: object) -> float | None:
    """Return a great-circle distance in metres, or null when GPS data is unavailable."""
    if point is None:
        return None
    try:
        target_latitude, target_longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    latitude_1, longitude_1 = map(math.radians, point)
    latitude_2, longitude_2 = map(math.radians, (target_latitude, target_longitude))
    delta_latitude, delta_longitude = latitude_2 - latitude_1, longitude_2 - longitude_1
    arc = math.sin(delta_latitude / 2) ** 2 + math.cos(latitude_1) * math.cos(latitude_2) * math.sin(delta_longitude / 2) ** 2
    return 6_371_000 * 2 * math.atan2(math.sqrt(arc), math.sqrt(1 - arc))


def endpoint_distance(source: object, endpoint: str, latitude: object, longitude: object) -> float | None:
    return geo_distance_meters(provider_endpoint(source, endpoint), latitude, longitude)


def endpoint_within(source: object, endpoint: str, latitude: object, longitude: object, radius_meters: object) -> bool:
    distance = endpoint_distance(source, endpoint, latitude, longitude)
    try:
        return distance is not None and distance <= float(radius_meters)
    except (TypeError, ValueError):
        return False


def import_connection(connection_row: Any) -> int:
    with connection() as db:
        if connection_row["provider_type"] == "HAMMERHEAD":
            connection_row = refresh_hammerhead_connection_token(db, connection_row)
        last = db.execute(
            "SELECT MAX(started_at_epoch) FROM provider_activities WHERE connection_id = ?",
            (connection_row["id"],),
        ).fetchone()[0]
    fetcher = fetch_hammerhead_activities if connection_row["provider_type"] == "HAMMERHEAD" else fetch_activities
    try:
        records = fetcher(
            connection_row["endpoint_url"] or "",
            connection_row["access_token"],
            connection_row["external_account_id"] or "",
            after=int(last) if last else None,
        )
    except HTTPError as error:
        if connection_row["provider_type"] != "HAMMERHEAD" or error.code != 401:
            raise
        # A revoked or prematurely expired access token can return 401 before
        # its advertised expiry. Refresh once, then retry this same import.
        with connection() as db:
            connection_row = refresh_hammerhead_connection_token(db, connection_row, force=True)
        records = fetcher(
            connection_row["endpoint_url"] or "",
            connection_row["access_token"],
            connection_row["external_account_id"] or "",
            after=int(last) if last else None,
        )
    imported = 0
    try:
        selected_types = set(json.loads(connection_row["activity_types_json"] or "[]"))
    except (KeyError, TypeError, json.JSONDecodeError):
        selected_types = set()
    with connection() as db:
        # Reconcile existing imports when the connection's type filter changed.
        # An empty selection intentionally means "all types".
        if selected_types:
            stale_provider_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM provider_activities WHERE connection_id = ? AND (sport_type IS NULL OR sport_type NOT IN ({}))".format(
                        ",".join("?" for _ in selected_types)
                    ), (connection_row["id"], *selected_types)
                ).fetchall()
            ]
            for provider_id in stale_provider_ids:
                linked_activity_ids = [
                    row["activity_id"] for row in db.execute(
                        "SELECT activity_id FROM activity_provider_links WHERE provider_activity_id = ?",
                        (provider_id,),
                    ).fetchall()
                ]
                db.execute("DELETE FROM activity_provider_links WHERE provider_activity_id = ?", (provider_id,))
                db.execute("DELETE FROM provider_activities WHERE id = ?", (provider_id,))
                for activity_id in linked_activity_ids:
                    remaining = db.execute(
                        "SELECT 1 FROM activity_provider_links WHERE activity_id = ? LIMIT 1", (activity_id,)
                    ).fetchone()
                    if remaining is None:
                        db.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        for activity in records:
            activity_type = activity.get("sport_type") or activity.get("type")
            if selected_types and activity_type not in selected_types:
                continue
            external_id = str(activity.get("id") or "")
            if not external_id:
                continue
            started = epoch_from_activity(activity)
            displayed_start = display_started_at(activity, connection_row["provider_type"])
            exists = db.execute(
                "SELECT id FROM provider_activities WHERE connection_id = ? AND external_activity_id = ?",
                (connection_row["id"], external_id),
            ).fetchone()
            if exists:
                # Hammerhead list imports are enriched with the Activity detail
                # (including its polyline) on later syncs as well.
                db.execute(
                    """UPDATE provider_activities
                       SET name=?, sport_type=?, started_at=?, started_at_epoch=?, distance_m=?, raw_json=?
                       WHERE id=?""",
                    (
                        activity.get("name") or "Unnamed activity",
                        activity.get("sport_type") or activity.get("type"),
                        displayed_start, started,
                        float(activity.get("distance") or 0), json.dumps(activity), exists["id"],
                    ),
                )
                # Re-run canonical linking after provider metadata changes.
                # This resolves records that were initially imported with a
                # provider-specific completion timestamp instead of a start.
                previous_ids = [row["activity_id"] for row in db.execute(
                    "SELECT activity_id FROM activity_provider_links WHERE provider_activity_id = ?", (exists["id"],)
                ).fetchall()]
                # Exclude its current canonical Activity. Otherwise an exact
                # self-match wins before we can compare the record to another
                # provider's near-identical activity.
                canonical_id = find_matching_canonical(
                    db, activity, started, exclude_ids=previous_ids, source_connection_id=connection_row["id"],
                )
                if canonical_id is None:
                    canonical_id = previous_ids[0] if previous_ids else find_or_create_canonical(
                        db, activity, started, displayed_start_at=displayed_start, source_connection_id=connection_row["id"],
                    )
                db.execute("DELETE FROM activity_provider_links WHERE provider_activity_id = ?", (exists["id"],))
                db.execute("INSERT OR IGNORE INTO activity_provider_links (activity_id, provider_activity_id) VALUES (?, ?)", (canonical_id, exists["id"]))
                if connection_row["provider_type"] == "HAMMERHEAD":
                    fetch_missing_hammerhead_hardware(db, connection_row, exists["id"], external_id)
                apply_rule(db, canonical_id, activity_type, connection_row["external_account_id"])
                for previous_id in previous_ids:
                    if previous_id != canonical_id and db.execute("SELECT 1 FROM activity_provider_links WHERE activity_id=? LIMIT 1", (previous_id,)).fetchone() is None:
                        db.execute("DELETE FROM activity_bike_assignments WHERE activity_id=?", (previous_id,))
                        db.execute("DELETE FROM activities WHERE id=?", (previous_id,))
                continue
            # The summary feed is intentionally fast. Load the one new
            # Hammerhead activity's detail object to persist its polyline.
            if connection_row["provider_type"] == "HAMMERHEAD":
                try:
                    activity = fetch_hammerhead_activity_detail(
                        connection_row["endpoint_url"], connection_row["access_token"], external_id
                    )
                    started = epoch_from_activity(activity)
                    displayed_start = display_started_at(activity, connection_row["provider_type"])
                except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
                    pass
            cursor = db.execute(
                """
                INSERT INTO provider_activities
                  (connection_id, external_activity_id, name, sport_type, started_at, started_at_epoch,
                   distance_m, raw_json, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    connection_row["id"], external_id, activity.get("name") or "Unnamed activity",
                    activity.get("sport_type") or activity.get("type"),
                    displayed_start, started,
                    float(activity.get("distance") or 0), json.dumps(activity),
                ),
            )
            provider_activity_id = cursor.lastrowid
            canonical_id = find_or_create_canonical(
                db, activity, started, displayed_start_at=displayed_start, source_connection_id=connection_row["id"],
            )
            db.execute(
                "INSERT OR IGNORE INTO activity_provider_links (activity_id, provider_activity_id) VALUES (?, ?)",
                (canonical_id, provider_activity_id),
            )
            if connection_row["provider_type"] == "HAMMERHEAD":
                fetch_missing_hammerhead_hardware(db, connection_row, provider_activity_id, external_id)
            apply_rule(db, canonical_id, activity_type, connection_row["external_account_id"])
            imported += 1
    return imported


def apply_rule(db: Any, activity_id: int, sport_type: str | None, account_id: str | None) -> None:
    activity_row = db.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if activity_row is None or activity_row["deleted_at"] is not None:
        return
    garage_start = db.execute(
        "SELECT mileage_tracking_started_at_epoch FROM user_settings ORDER BY user_id LIMIT 1"
    ).fetchone()
    garage_start_epoch = garage_start["mileage_tracking_started_at_epoch"] if garage_start else None
    if garage_start_epoch is not None and (activity_row["started_at_epoch"] or 0) < garage_start_epoch:
        # Keep imported history untouched. It is outside the Garage's managed
        # period and must not receive automatic titles or bike assignments.
        return
    rules = db.execute(
        "SELECT * FROM resolver_rules WHERE enabled = 1 ORDER BY priority ASC, id ASC"
    ).fetchall()
    linked_count = db.execute(
        "SELECT COUNT(*) FROM activity_provider_links WHERE activity_id = ?", (activity_id,)
    ).fetchone()[0]
    rows = db.execute("""SELECT pa.id AS provider_activity_id, pc.provider_type AS provider, pc.identifier AS connection, pc.external_account_id AS account, pa.name AS title,
           pa.sport_type AS sport_type, pa.distance_m AS distance_m, pa.started_at AS started_at, pa.raw_json AS raw_json FROM activity_provider_links apl
           JOIN provider_activities pa ON pa.id = apl.provider_activity_id
           JOIN provider_connections pc ON pc.id = pa.connection_id
           WHERE apl.activity_id = ?""", (activity_id,)).fetchall()
    rows = attach_hardware_observations(db, rows)
    bike_rows = [dict(row) for row in db.execute("SELECT * FROM bikes ORDER BY name").fetchall()]
    for bike in bike_rows:
        bike["components"] = [dict(component) for component in db.execute(
            "SELECT component_type,ant_device_number FROM bike_components WHERE bike_id=?", (bike["id"],)
        ).fetchall()]
    assigned_rows = db.execute("SELECT b.* FROM activity_bike_assignments aba JOIN bikes b ON b.id=aba.bike_id WHERE aba.activity_id=? ORDER BY aba.slot_index", (activity_id,)).fetchall()
    context = ActivityContext(rows, dict(activity_row) if activity_row else None, bike_rows, [dict(row) for row in assigned_rows])
    count = 1
    resolution_id = uuid.uuid4().hex
    for rule in rules:
        rule_logs: list[str] = []
        result = evaluate_expression(rule["expression"], context, rule_logs)
        applied = result is not None and result is not False
        result_text = "null" if result is None else str(result).lower() if isinstance(result, bool) else str(result)
        db.execute(
            """INSERT INTO activity_rule_runs
               (activity_id,resolution_id,rule_id,rule_name,result_text,log_output_json,applied)
               VALUES (?,?,?,?,?,?,?)""",
            (activity_id, resolution_id, rule["id"], rule["name"], result_text, json.dumps(rule_logs), int(applied)),
        )
        if result is None or result is False:
            continue
        if result is True:
            result = linked_count
        count = max(1, result) if isinstance(result, int) else result
        break
    # Keep a useful local history without letting periodic syncs grow the
    # database indefinitely. A resolution contains one row per evaluated rule.
    obsolete_runs = db.execute(
        """SELECT resolution_id FROM activity_rule_runs WHERE activity_id=?
           GROUP BY resolution_id ORDER BY MAX(id) DESC LIMIT -1 OFFSET 30""",
        (activity_id,),
    ).fetchall()
    if obsolete_runs:
        placeholders = ",".join("?" for _ in obsolete_runs)
        db.execute(
            f"DELETE FROM activity_rule_runs WHERE activity_id=? AND resolution_id IN ({placeholders})",
            (activity_id, *(row["resolution_id"] for row in obsolete_runs)),
        )
    resolved_count = max(1, int(context.bikeCount or count))
    resolved_title = activity_row["manual_title"] or context.title or activity_row["name"]
    db.execute("UPDATE activities SET name = ?, expected_bike_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (resolved_title, resolved_count, activity_id))
    if context.bikes_changed:
        db.execute("DELETE FROM activity_bike_assignments WHERE activity_id=? AND source='RULE'", (activity_id,))
        for slot_index, bike in enumerate(context.bikes[:resolved_count], start=1):
            if bike and bike.id:
                db.execute("INSERT OR REPLACE INTO activity_bike_assignments (activity_id,bike_id,slot_index,source) VALUES (?,?,?,'RULE')", (activity_id,bike.id,slot_index))
    # Resolver runs only calculate Bike Garage state. External Strava writes
    # require an explicit confirmation from the activity detail page.


def apply_rule_assignment(line: str, context: ActivityContext, names: dict[str, object], functions: dict[str, object]) -> bool:
    match = re.match(r"^activity\.title\s*=\s*(.+)$", line)
    if match:
        title = evaluate_formula(match.group(1), names, functions)
        if title is None:
            raise ValueError
        context.title = title
        return True
    match = re.match(r"^activity\.bikeCount\s*=\s*(.+)$", line)
    if match:
        proposed_count = max(1, int(evaluate_formula(match.group(1), names, functions)))
        if len([bike for bike in context.bikes if bike is not None]) > proposed_count:
            raise ValueError("Bike assignment exceeds bikeCount")
        context.bikeCount = proposed_count
        return True
    match = re.match(r"^activity\.bikes\s*=\s*(.+)$", line)
    if match:
        value = evaluate_formula(match.group(1), names, functions)
        # A single lookup is the natural form for the common case:
        # `activity.bikes = bikes.find(identifier="crux")`.  Normalize it to
        # the same list representation used by multi-bike assignments.
        if value is None or isinstance(value, Bike):
            value = [value]
        if not isinstance(value, list) or not all(item is None or isinstance(item, Bike) for item in value):
            raise ValueError
        assigned_bikes = [item for item in value if item is not None]
        if len(assigned_bikes) > context.bikeCount:
            raise ValueError("Bike assignment exceeds bikeCount")
        context.bikes = assigned_bikes
        return True
    match = re.match(r"^activity\.bikes\[(\d+)\]\s*=\s*(.+)$", line)
    if match:
        index, value = int(match.group(1)), evaluate_formula(match.group(2), names, functions)
        if value is not None and not isinstance(value, Bike): raise ValueError
        if index >= context.bikeCount:
            raise ValueError("Bike assignment exceeds bikeCount")
        while len(context.bikes) <= index: context.bikes.append(None)
        context.bikes[index] = value
        context.bikes_changed = True
        return True
    return False


def apply_collection_statement(line: str, names: dict[str, object], functions: dict[str, object]) -> bool:
    declaration = re.match(r"^(?:let|var)\s+([A-Za-z_]\w*)\s*=\s*(.+)$", line)
    if declaration:
        variable, formula = declaration.groups()
        if variable in functions: raise ValueError
        value = evaluate_formula(formula, names, functions)
        names[variable] = RuleList(value) if isinstance(value, list) else value
        return True
    reassignment = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.+)$", line)
    if reassignment:
        variable, formula = reassignment.groups()
        # Reassignment deliberately requires an existing variable. This keeps
        # typos visible while allowing natural fallbacks such as
        # `bike = bikes.find(antDeviceNumber=power.antDeviceNumber)`.
        if variable not in names or variable in functions:
            raise ValueError
        value = evaluate_formula(formula, names, functions)
        names[variable] = RuleList(value) if isinstance(value, list) else value
        return True
    append = re.match(r"^([A-Za-z_]\w*)\.append\((.*)\)$", line)
    if append:
        variable, formula = append.groups()
        if variable not in names or not isinstance(names[variable], list): raise ValueError
        names[variable].append(evaluate_formula(formula, names, functions))
        return True
    return False


def append_print_log(inner: str, names: dict[str, object], functions: dict[str, object], logs: list[str] | None) -> None:
    """Evaluate print arguments as expressions, including nested calls."""
    if logs is None:
        return
    parsed = ast.parse(f"_print({inner})", mode="eval").body
    if not isinstance(parsed, ast.Call):
        raise ValueError
    values = [evaluate_formula(ast.unparse(argument), names, functions) for argument in parsed.args]
    logs.append(" ".join("null" if value is None else str(value) for value in values))


def evaluate_expression(expression: str, context: ActivityContext, logs: list[str] | None = None) -> int | None:
    try:
        names = {"activity": context, "bikes": context.availableBikes, "provider_activity_count": context.providerActivities.count,
                 "hammerhead_activity_count": context.providerActivities.filter(provider="HAMMERHEAD").count}
        functions = {
            "max": max,
            "min": min,
            "coalesce": lambda *values: next((value for value in values if value not in (None, "")), ""),
            "print": lambda *args: (logs.extend(str(a) for a in args) if logs is not None else None) or 0,
            "startDistanceTo": lambda source, latitude, longitude: endpoint_distance(source, "start", latitude, longitude),
            "endDistanceTo": lambda source, latitude, longitude: endpoint_distance(source, "end", latitude, longitude),
            "startsWithin": lambda source, latitude, longitude, radius_meters: endpoint_within(source, "start", latitude, longitude, radius_meters),
            "endsWithin": lambda source, latitude, longitude, radius_meters: endpoint_within(source, "end", latitude, longitude, radius_meters),
            "endpointsWithin": lambda source, start_latitude, start_longitude, end_latitude, end_longitude, radius_meters: (
                endpoint_within(source, "start", start_latitude, start_longitude, radius_meters)
                and endpoint_within(source, "end", end_latitude, end_longitude, radius_meters)
            ),
        }
        lines: list[str] = []
        indents: list[int] = []
        pending = ""
        pending_indent = 0
        for raw_line in expression.replace("linked_provider_activities", "provider_activity_count").splitlines():
            uncommented = strip_rule_comment(raw_line)
            indent = len(uncommented) - len(uncommented.lstrip())
            line = uncommented.strip()
            if not line: continue
            if line in ("{", "}"): continue
            function_header = re.match(r"^(?:function|def)\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*(?::|\{)?$", line)
            if function_header:
                # Functions use the same indentation rules as `if`; both
                # `function bikeFor(source) {` and `function bikeFor(source):`
                # are accepted.
                line = f"function {function_header.group(1)}({function_header.group(2)}):"
            if line.startswith("} else"):
                line = "else:"
            else:
                brace_if = re.match(r"^if\s*\((.*)\)\s*\{$", line)
                if brace_if: line = f"if {brace_if.group(1)}:"
                elif line.endswith("{"): line = line[:-1].rstrip()
            if not pending:
                pending_indent = indent
            pending = f"{pending} {line}".strip()
            if pending.count("(") > pending.count(")"):
                continue
            # Permit compact one-line statements such as: print("x") return true
            parts = re.split(r"(?<=\))\s+(?=return\s+)", pending)
            for part in parts:
                if part.strip():
                    lines.append(part.strip())
                    indents.append(pending_indent)
            pending = ""
        if pending:
            lines.append(pending)
            indents.append(pending_indent)
        if not lines:
            raise ValueError
        def return_value(line: str, scope: dict[str, object]) -> object:
            value = line[7:].strip()
            if value == "null": return None
            if value == "true": return True
            if value == "false": return False
            return evaluate_formula(value, scope, functions)

        def execute_block(start: int, end: int, scope: dict[str, object] | None = None) -> tuple[bool, object | None]:
            """Interpret a same-indentation statement range, recursively.

            Earlier V1 code flattened only the first IF/ELSE group. A nested
            condition then became an unknown statement, so execution stopped
            after the first outer print. Keeping the original indentation lets
            the small DSL behave like a normal block language.
            """
            scope = names if scope is None else scope
            index = start
            while index < end:
                line = lines[index]
                function_match = re.match(r"^function\s+([A-Za-z_]\w*)\s*\((.*?)\):$", line)
                if function_match:
                    base_indent = indents[index]
                    function_end = index + 1
                    while function_end < end and indents[function_end] > base_indent:
                        function_end += 1
                    function_name, raw_params = function_match.groups()
                    parameters = [item.strip() for item in raw_params.split(",") if item.strip()]
                    if (function_name in functions or function_name in scope or
                            any(not re.fullmatch(r"[A-Za-z_]\w*", parameter) for parameter in parameters)):
                        raise ValueError
                    if function_end == index + 1:
                        raise ValueError
                    definition_scope = dict(scope)
                    body_start, body_end = index + 1, function_end
                    def user_function(*arguments: object, _parameters=parameters, _scope=definition_scope,
                                      _start=body_start, _end=body_end) -> object:
                        if len(arguments) != len(_parameters):
                            raise ValueError
                        local_scope = dict(_scope)
                        local_scope.update(zip(_parameters, arguments))
                        did_return, result = execute_block(_start, _end, local_scope)
                        return result if did_return else None
                    functions[function_name] = user_function
                    index = function_end
                    continue
                if line.startswith("if ") and line.endswith(":"):
                    base_indent = indents[index]
                    clauses: list[tuple[int, str | None]] = [(index, line[3:-1].strip())]
                    cursor = index + 1
                    block_end = end
                    while cursor < end:
                        if indents[cursor] < base_indent:
                            block_end = cursor
                            break
                        if indents[cursor] == base_indent:
                            candidate = lines[cursor]
                            if candidate.startswith("else if ") and candidate.endswith(":"):
                                clauses.append((cursor, candidate[8:-1].strip()))
                            elif candidate == "else:":
                                clauses.append((cursor, None))
                            else:
                                block_end = cursor
                                break
                        cursor += 1
                    selected = next(
                        (position for position, condition in clauses
                         if condition is None or evaluate_formula(condition, scope, functions)),
                        None,
                    )
                    if selected is not None:
                        next_header = next((position for position, _ in clauses if position > selected), block_end)
                        did_return, result = execute_block(selected + 1, next_header, scope)
                        if did_return:
                            return True, result
                    index = block_end
                    continue
                if line.startswith(("else if ", "else:")):
                    raise ValueError
                if apply_rule_assignment(line, context, scope, functions):
                    index += 1
                    continue
                if apply_collection_statement(line, scope, functions):
                    index += 1
                    continue
                if line.startswith("print("):
                    append_print_log(line[6:-1], scope, functions, logs)
                    index += 1
                    continue
                if line.startswith("return "):
                    return True, return_value(line, scope)
                # A helper may be called for its side effects, for example a
                # function that resolves and assigns the bike itself.
                if re.fullmatch(r"[A-Za-z_]\w*\s*\(.*\)", line):
                    evaluate_formula(line, scope, functions)
                    index += 1
                    continue
                # A final bare formula is convenient for compact rules.
                if index == end - 1:
                    return True, int(evaluate_formula(line, scope, functions))
                raise ValueError
            return False, None

        did_return, result = execute_block(0, len(lines))
        return result if did_return else False
    except (SyntaxError, ValueError, TypeError, AttributeError, ZeroDivisionError):
        return max(1, context.providerActivities.count)


def evaluate_formula(formula: str, names: dict[str, int], functions: dict[str, object]) -> int:
    # The expression editor can leave whitespace after a dot while completing
    # a property (for example `sensor). antDeviceNumber`). Treat it like the
    # intended member access instead of silently abandoning the whole rule.
    formula = re.sub(r"\.\s+([A-Za-z_])", r".\1", formula)
    formula = re.sub(r"\bnull\b", "None", formula)
    formula = re.sub(r"\btrue\b", "True", formula)
    formula = re.sub(r"\bfalse\b", "False", formula)
    formula = formula.replace("&&", " and ").replace("||", " or ")
    tree = ast.parse(formula, mode="eval")
    allowed = (ast.Expression, ast.Constant, ast.Name, ast.Attribute, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult,
               ast.Div, ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd, ast.Call, ast.Load, ast.keyword, ast.Compare,
               ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq, ast.BoolOp, ast.And, ast.Or, ast.List,
               ast.Subscript)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError
        if isinstance(node, ast.Name) and node.id not in names and node.id not in functions:
            raise ValueError
        if isinstance(node, ast.Call) and not ((isinstance(node.func, ast.Name) and node.func.id in functions) or
                                                (isinstance(node.func, ast.Attribute) and node.func.attr in ("filter", "find", "hardware"))):
            raise ValueError
    return eval(compile(tree, "<count-rule>", "eval"), {"__builtins__": {}}, {**names, **functions})

def bike_assignment_limit_error(expression: str) -> str | None:
    """Catch statically knowable bike assignments that overflow bikeCount."""
    bike_count: int | None = None
    arrays: dict[str, int] = {}
    pending = ""
    for raw_line in expression.splitlines():
        line = strip_rule_comment(raw_line).strip()
        if not line or line in ("{", "}") or line.endswith(":") or line.startswith(("if ", "else")):
            continue
        pending = f"{pending} {line}".strip()
        if pending.count("(") > pending.count(")") or pending.count("[") > pending.count("]"):
            continue
        line, pending = pending, ""
        count_match = re.match(r"^activity\.bikeCount\s*=\s*(\d+)\s*$", line)
        if count_match:
            bike_count = max(1, int(count_match.group(1)))
            continue
        declaration = re.match(r"^(?:let|var)\s+([A-Za-z_]\w*)\s*=\s*(.+)$", line)
        if declaration:
            variable, value = declaration.groups()
            try:
                parsed = ast.parse(value, mode="eval").body
                if isinstance(parsed, ast.List): arrays[variable] = len(parsed.elts)
            except SyntaxError:
                pass
            continue
        append = re.match(r"^([A-Za-z_]\w*)\.append\(.*\)$", line)
        if append and append.group(1) in arrays:
            arrays[append.group(1)] += 1
            continue
        assignment = re.match(r"^activity\.bikes\s*=\s*(.+)$", line)
        indexed_assignment = re.match(r"^activity\.bikes\[(\d+)\]\s*=", line)
        assigned_count: int | None = None
        if assignment:
            value = assignment.group(1).strip()
            if value in arrays:
                assigned_count = arrays[value]
            else:
                try:
                    parsed = ast.parse(value, mode="eval").body
                    if isinstance(parsed, ast.List): assigned_count = len(parsed.elts)
                except SyntaxError:
                    pass
        elif indexed_assignment:
            assigned_count = int(indexed_assignment.group(1)) + 1
        if bike_count is not None and assigned_count is not None and assigned_count > bike_count:
            return f"Bike assignment has {assigned_count} bikes, but activity.bikeCount is {bike_count}. Increase bikeCount or remove a bike."
    return None


def validate_expression(expression: str) -> bool:
    try:
        if bike_assignment_limit_error(expression):
            return False
        lines = []
        pending = ""
        for raw in expression.replace("linked_provider_activities", "provider_activity_count").splitlines():
            line = strip_rule_comment(raw).strip()
            if not line: continue
            if line in ("{", "}"): continue
            function_header = re.match(r"^(?:function|def)\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*(?::|\{)?$", line)
            if function_header:
                line = f"function {function_header.group(1)}({function_header.group(2)}):"
            if line.startswith("} else"): line = "else:"
            else:
                brace_if = re.match(r"^if\s*\((.*)\)\s*\{$", line)
                if brace_if: line = f"if {brace_if.group(1)}:"
                elif line.endswith("{"): line = line[:-1].rstrip()
            pending = f"{pending} {line}".strip()
            if pending.count("(") > pending.count(")"): continue
            lines.append(pending); pending = ""
        if pending: lines.append(pending)
        if not lines:
            return False
        local_names = set()
        for line in lines:
            function_header = re.match(r"^function\s+([A-Za-z_]\w*)\s*\((.*?)\):$", line)
            if function_header:
                function_name, raw_parameters = function_header.groups()
                parameters = [item.strip() for item in raw_parameters.split(",") if item.strip()]
                if (function_name in local_names or
                        any(not re.fullmatch(r"[A-Za-z_]\w*", parameter) for parameter in parameters)):
                    return False
                local_names.add(function_name)
                local_names.update(parameters)
                continue
            declaration = re.match(r"^(?:let|var)\s+([A-Za-z_]\w*)\s*=\s*(.+)$", line)
            if declaration:
                local_names.add(declaration.group(1))
                source = declaration.group(2).strip()
            else:
                source = line
            reassignment = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.+)$", source)
            if reassignment:
                if reassignment.group(1) not in local_names:
                    return False
                source = reassignment.group(2).strip()
            append = re.match(r"^([A-Za-z_]\w*)\.append\((.*)\)$", source)
            if append:
                if append.group(1) not in local_names:
                    return False
                source = append.group(2).strip()
            assignment = re.match(r"^activity\.(?:title|bikeCount|bikes(?:\[\d+\])?)\s*=\s*(.+)$", source)
            if assignment:
                source = assignment.group(1)
            if source.startswith(("if ", "else if ", "else:")):
                if source.startswith("else if "):
                    source = source[8:].rstrip(":").strip()
                elif source.startswith("if "):
                    source = source[3:].rstrip(":").strip()
                else:
                    continue
            if source.startswith("return "): source = source[7:].strip()
            if source in ("null", "true", "false"): continue
            source = re.sub(r"\.\s+([A-Za-z_])", r".\1", source)
            source = re.sub(r"\bnull\b", "None", source)
            source = re.sub(r"\btrue\b", "True", source)
            source = re.sub(r"\bfalse\b", "False", source)
            source = source.replace("&&", " and ").replace("||", " or ")
            tree = ast.parse(source[:-1] if source.endswith(":") else source, mode="eval")
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr not in (
                    "providerActivities", "count", "filter", "first", "title", "provider",
                    "account", "connection", "sportType", "distance", "startedAt", "json", "title",
                    "bikeCount", "bikes", "availableBikes", "identifier", "name", "id", "find", "append",
                    "hardware", "antDeviceNumber", "componentType", "manufacturer", "serialNumber", "component"
                ):
                    return False
                if isinstance(node, ast.Name) and node.id not in (
                    "activity", "bikes", "provider_activity_count", "hammerhead_activity_count", "max", "min", "coalesce", "print",
                    "startDistanceTo", "endDistanceTo", "startsWithin", "endsWithin", "endpointsWithin",
                ):
                    if node.id not in local_names: return False
        return True
    except (SyntaxError, ValueError, TypeError):
        return False


def expression_error(expression: str) -> str:
    """Return a useful editor-facing reason when DSL validation fails."""
    limit_error = bike_assignment_limit_error(expression)
    if limit_error:
        return limit_error
    raw_lines = expression.splitlines()
    declared = {"activity", "bikes", "provider_activity_count", "hammerhead_activity_count",
                "max", "min", "coalesce", "print", "startDistanceTo", "endDistanceTo", "startsWithin", "endsWithin", "endpointsWithin"}
    allowed_properties = {
        "providerActivities", "count", "filter", "first", "title", "provider", "account", "connection",
        "sportType", "distance", "startedAt", "json", "bikeCount", "bikes", "availableBikes", "identifier",
        "name", "id", "find", "append", "hardware", "antDeviceNumber", "componentType", "manufacturer",
        "serialNumber", "component",
    }

    def failure(line_number: int, detail: str) -> str:
        source = raw_lines[line_number - 1].strip() if line_number <= len(raw_lines) else ""
        suffix = f" — `{source}`" if source else ""
        return f"Invalid expression on line {line_number}: {detail}{suffix}"

    statements: list[tuple[int, str]] = []
    pending = ""
    pending_line = 0
    for line_number, raw_line in enumerate(raw_lines, start=1):
        line = strip_rule_comment(raw_line).strip()
        if not line or line in ("{", "}"):
            continue
        function_header = re.match(r"^(?:function|def)\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*(?::|\{)?$", line)
        if function_header:
            line = f"function {function_header.group(1)}({function_header.group(2)}):"
        if line.startswith("} else"):
            line = "else:"
        else:
            brace_if = re.match(r"^if\s*\((.*)\)\s*\{$", line)
            if brace_if:
                line = f"if {brace_if.group(1)}:"
            elif line.endswith("{"):
                line = line[:-1].rstrip()
        if not pending:
            pending_line = line_number
        pending = f"{pending} {line}".strip()
        if pending.count("(") > pending.count(")") or pending.count("[") > pending.count("]"):
            continue
        statements.append((pending_line, pending))
        pending = ""
    if pending:
        return failure(pending_line, "unclosed function call, list, or parenthesis")

    for line_number, statement in statements:
        function_header = re.match(r"^function\s+([A-Za-z_]\w*)\s*\((.*?)\):$", statement)
        if function_header:
            function_name, raw_parameters = function_header.groups()
            parameters = [item.strip() for item in raw_parameters.split(",") if item.strip()]
            if function_name in declared:
                return failure(line_number, f"`{function_name}` is already declared")
            invalid_parameter = next((item for item in parameters if not re.fullmatch(r"[A-Za-z_]\w*", item)), None)
            if invalid_parameter:
                return failure(line_number, f"`{invalid_parameter}` is not a valid function parameter")
            declared.add(function_name)
            declared.update(parameters)
            continue
        declaration = re.match(r"^(?:let|var)\s+([A-Za-z_]\w*)\s*=\s*(.+)$", statement)
        source = declaration.group(2).strip() if declaration else statement
        if declaration:
            declared.add(declaration.group(1))
        reassignment = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.+)$", source)
        if reassignment:
            variable, source = reassignment.groups()
            if variable not in declared:
                suggestion = difflib.get_close_matches(variable, declared, n=1)
                hint = f" Did you mean `{suggestion[0]}`?" if suggestion else ""
                return failure(line_number, f"`{variable}` is not declared.{hint}")
            source = source.strip()
        append = re.match(r"^([A-Za-z_]\w*)\.append\((.*)\)$", source)
        if append:
            variable = append.group(1)
            if variable not in declared:
                suggestion = difflib.get_close_matches(variable, declared, n=1)
                hint = f" Did you mean `{suggestion[0]}`?" if suggestion else ""
                return failure(line_number, f"`{variable}` is not a declared array.{hint}")
            source = append.group(2).strip()
        assignment = re.match(r"^activity\.(?:title|bikeCount|bikes(?:\[\d+\])?)\s*=\s*(.+)$", source)
        if assignment:
            source = assignment.group(1)
        if source.startswith("else if "):
            source = source[8:].rstrip(":").strip()
        elif source.startswith("if "):
            source = source[3:].rstrip(":").strip()
        elif source == "else:":
            continue
        if source.startswith("return "):
            source = source[7:].strip()
        if source in ("null", "true", "false"):
            continue
        source = re.sub(r"\.\s+([A-Za-z_])", r".\1", source)
        source = re.sub(r"\bnull\b", "None", source)
        source = re.sub(r"\btrue\b", "True", source)
        source = re.sub(r"\bfalse\b", "False", source)
        source = source.replace("&&", " and ").replace("||", " or ")
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as error:
            return failure(line_number, f"syntax error ({error.msg})")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr not in allowed_properties:
                return failure(line_number, f"`{node.attr}` is not available on this object")
            if isinstance(node, ast.Name) and node.id not in declared and node.id not in {"None", "True", "False"}:
                suggestion = difflib.get_close_matches(node.id, declared, n=1)
                hint = f" Did you mean `{suggestion[0]}`?" if suggestion else ""
                return failure(line_number, f"`{node.id}` is not declared.{hint}")
    return "Invalid expression: check syntax, property names, and declared variables."


def find_matching_canonical(
    db: Any, activity: dict[str, Any], started: int | None, *, exclude_ids: list[int] | None = None,
    source_connection_id: int | None = None,
) -> int | None:
    distance = float(activity.get("distance") or 0)
    excluded = exclude_ids or []
    exclusion = f" AND id NOT IN ({','.join('?' for _ in excluded)})" if excluded else ""
    sport_type = activity.get("sport_type") or activity.get("type") or ""
    # Providers do not agree on cycling subtypes. A Karoo record often says
    # `Ride` while the matching Strava activity says `GravelRide`; they still
    # describe the same physical ride. Keep virtual rides out of this family.
    cycling_types = ("Ride", "GravelRide", "MountainBikeRide", "EMountainBikeRide", "EBikeRide")
    compatible_types = cycling_types if sport_type in cycling_types else (sport_type,)
    sport_placeholders = ",".join("?" for _ in compatible_types)
    candidates = db.execute(
        f"SELECT * FROM activities WHERE deleted_at IS NULL AND sport_type IN ({sport_placeholders}){exclusion} "
        "ORDER BY started_at_epoch DESC LIMIT 100",
        (*compatible_types, *excluded),
    ).fetchall()
    for candidate in candidates:
        if started is None or candidate["started_at_epoch"] is None:
            continue
        # One source feed is authoritative about its own individual rides.
        # Similar timestamps from two records in the same connection are not
        # corroborating evidence of one ride and must remain distinct.
        if source_connection_id is not None and db.execute(
            """SELECT 1 FROM activity_provider_links apl
               JOIN provider_activities pa ON pa.id=apl.provider_activity_id
               WHERE apl.activity_id=? AND pa.connection_id=? LIMIT 1""",
            (candidate["id"], source_connection_id),
        ).fetchone() is not None:
            continue
        candidate_distance = float(candidate["distance_m"] or 0)
        tolerance = max(1000.0, distance * 0.05)
        # A canonical Activity may have been created from an older provider
        # timestamp (notably legacy Hammerhead imports). Compare against every
        # linked provider timestamp as well; those records are the actual
        # evidence used for matching.
        linked_times = [candidate["started_at_epoch"]]
        for row in db.execute(
            """SELECT pa.started_at_epoch FROM activity_provider_links apl
               JOIN provider_activities pa ON pa.id=apl.provider_activity_id
               WHERE apl.activity_id=? AND pa.started_at_epoch IS NOT NULL""",
            (candidate["id"],),
        ).fetchall():
            linked_times.append(row["started_at_epoch"])
        time_delta = min(abs(started - timestamp) for timestamp in linked_times if timestamp is not None)
        distance_delta = abs(candidate_distance - distance)
        # A normal match is a 15 minute window. Hammerhead summaries may only
        # carry completion time plus duration, while Strava records UTC start
        # time. Accept a wider window only for a near-identical route length.
        # Fifteen minutes of slack avoids a brittle boundary for clocks and
        # provider-side timestamp rounding while keeping the distance check
        # extremely strict.
        normal_match = time_delta <= 15 * 60 and distance_delta <= tolerance
        strong_distance_match = time_delta <= 4 * 60 * 60 + 15 * 60 and distance_delta <= max(100.0, distance * 0.0025)
        # A group ride can have two legitimate recordings whose distances vary
        # noticeably: e.g. two riders start together but one device pauses
        # later, trims GPS data, or takes a short detour.  This is deliberately
        # much narrower in time than the normal provider match, so it cannot
        # turn unrelated rides into a match merely because their distances are
        # similar.  It only applies to cycling types (see compatible_types
        # above), rides of meaningful length, and records started within three
        # minutes of each other.
        linked_source_count = db.execute(
            "SELECT COUNT(*) FROM activity_provider_links WHERE activity_id=?", (candidate["id"],)
        ).fetchone()[0]
        concurrent_group_match = (
            # Requiring an already corroborated target makes a wider window
            # safe for partners whose devices were started a little apart.
            linked_source_count >= 2
            and time_delta <= 20 * 60
            and distance >= 1_000.0
            and candidate_distance >= 1_000.0
            and distance_delta <= max(2_500.0, max(distance, candidate_distance) * 0.15)
        )
        if normal_match or strong_distance_match or concurrent_group_match:
            db.execute(
                "UPDATE activities SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (candidate["id"],)
            )
            return candidate["id"]
    return None


def refresh_canonical_timing(db: Any, activity_id: int) -> None:
    """Use the best available source time for the canonical display record."""
    preferred = db.execute(
        """SELECT pa.started_at, pa.started_at_epoch
           FROM activity_provider_links apl
           JOIN provider_activities pa ON pa.id=apl.provider_activity_id
           JOIN provider_connections pc ON pc.id=pa.connection_id
           WHERE apl.activity_id=? AND pa.started_at_epoch IS NOT NULL
           ORDER BY CASE pc.provider_type WHEN 'STRAVA_PROXY' THEN 0 WHEN 'HAMMERHEAD' THEN 1 ELSE 2 END,
                    pa.started_at_epoch ASC
           LIMIT 1""",
        (activity_id,),
    ).fetchone()
    if preferred:
        db.execute(
            "UPDATE activities SET started_at=?, started_at_epoch=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (preferred["started_at"], preferred["started_at_epoch"], activity_id),
        )


def refresh_all_canonical_timings() -> None:
    """Repair display times of canonical records after provider enrichment."""
    with connection() as db:
        activity_ids = db.execute("SELECT id FROM activities").fetchall()
        for row in activity_ids:
            refresh_canonical_timing(db, row["id"])


def find_or_create_canonical(
    db: Any, activity: dict[str, Any], started: int | None, *, displayed_start_at: str | None = None,
    source_connection_id: int | None = None,
) -> int:
    canonical_id = find_matching_canonical(db, activity, started, source_connection_id=source_connection_id)
    if canonical_id is not None:
        return canonical_id
    distance = float(activity.get("distance") or 0)
    cursor = db.execute(
        """
        INSERT INTO activities (public_id, name, sport_type, started_at, started_at_epoch, distance_m)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()), activity.get("name") or "Unnamed activity", activity.get("sport_type") or activity.get("type"),
            displayed_start_at or activity.get("start_date_local") or activity.get("start_date"), started, distance,
        ),
    )
    return cursor.lastrowid


def repair_hammerhead_start_times() -> int:
    """Repair older imports created before `raw_hammerhead.startedAt` was used.

    Hammerhead's `createdAt` represents upload/completion time. Earlier V1
    imports derived a ride start from that timestamp, which can be wrong when
    an activity was uploaded later. The raw payload already contains the
    authoritative `startedAt`, so this migration is safe and idempotent.
    """
    repaired = 0
    with connection() as db:
        rows = db.execute(
            """SELECT pa.id, pa.started_at, pa.started_at_epoch, pa.raw_json
               FROM provider_activities pa
               JOIN provider_connections pc ON pc.id=pa.connection_id
               WHERE pc.provider_type='HAMMERHEAD'"""
        ).fetchall()
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
                started_at = (raw.get("raw_hammerhead") or {}).get("startedAt")
            except (TypeError, json.JSONDecodeError):
                started_at = None
            if not started_at:
                continue
            started_epoch = epoch_from_activity({"start_date": started_at})
            if started_epoch is None:
                continue
            if row["started_at"] == started_at and row["started_at_epoch"] == started_epoch:
                continue
            db.execute(
                "UPDATE provider_activities SET started_at=?, started_at_epoch=? WHERE id=?",
                (started_at, started_epoch, row["id"]),
            )
            repaired += 1
    return repaired


def repair_strava_local_start_times() -> int:
    """Correct local Strava timestamps imported before local-time handling.

    This is deliberately idempotent: the UTC epoch stays untouched for
    matching and sorting; only the display value is normalised from the raw
    provider payload.
    """
    repaired = 0
    with connection() as db:
        rows = db.execute(
            """SELECT pa.id, pa.started_at, pa.raw_json
               FROM provider_activities pa
               JOIN provider_connections pc ON pc.id=pa.connection_id
               WHERE pc.provider_type='STRAVA_PROXY'"""
        ).fetchall()
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            expected = display_started_at(raw, "STRAVA_PROXY")
            if not expected or row["started_at"] == expected:
                continue
            db.execute(
                "UPDATE provider_activities SET started_at=? WHERE id=?",
                (expected, row["id"]),
            )
            repaired += 1
    return repaired


def reconcile_single_source_activities() -> int:
    """Merge older one-source activities after the matching rules improve.

    Importing only new records should not leave historic near-identical rides
    permanently split. We intentionally consider only canonical activities
    with one provider record: already grouped rides are authoritative and are
    never candidates for an automatic merge here.
    """
    merged = 0
    with connection() as db:
        candidates = db.execute(
            """
            SELECT a.id AS activity_id, pa.connection_id, pa.sport_type, pa.distance_m, pa.started_at, pa.started_at_epoch
            FROM activities a
            JOIN activity_provider_links apl ON apl.activity_id = a.id
            JOIN provider_activities pa ON pa.id = apl.provider_activity_id
            GROUP BY a.id
            HAVING COUNT(apl.provider_activity_id) = 1
            """
        ).fetchall()
        for candidate in candidates:
            source_id = candidate["activity_id"]
            # It may already have been merged by an earlier candidate in this
            # pass, so always re-check before altering anything.
            if db.execute("SELECT 1 FROM activities WHERE id=?", (source_id,)).fetchone() is None:
                continue
            target_id = find_matching_canonical(
                db,
                {"sport_type": candidate["sport_type"], "distance": candidate["distance_m"]},
                candidate["started_at_epoch"],
                exclude_ids=[source_id], source_connection_id=candidate["connection_id"],
            )
            if target_id is None:
                continue
            # Preserve a rider's existing bike choice.  A source activity and
            # its target often both use slot 1; put the incoming bike in the
            # next free slot instead of silently dropping it.  `MERGED` keeps
            # that choice intact when the target's RULE assignments are
            # recalculated below.
            target_assignments = db.execute(
                "SELECT bike_id, slot_index FROM activity_bike_assignments WHERE activity_id=?",
                (target_id,),
            ).fetchall()
            used_bikes = {row["bike_id"] for row in target_assignments}
            used_slots = {row["slot_index"] for row in target_assignments}
            for assignment in db.execute(
                "SELECT bike_id, slot_index FROM activity_bike_assignments WHERE activity_id=? ORDER BY slot_index",
                (source_id,),
            ).fetchall():
                if assignment["bike_id"] in used_bikes:
                    continue
                slot_index = assignment["slot_index"]
                while slot_index in used_slots:
                    slot_index += 1
                db.execute(
                    """INSERT INTO activity_bike_assignments
                       (activity_id, bike_id, slot_index, source)
                       VALUES (?, ?, ?, 'MERGED')""",
                    (target_id, assignment["bike_id"], slot_index),
                )
                used_bikes.add(assignment["bike_id"])
                used_slots.add(slot_index)
            db.execute(
                "UPDATE activity_provider_links SET activity_id=? WHERE activity_id=?",
                (target_id, source_id),
            )
            db.execute("DELETE FROM activities WHERE id=?", (source_id,))
            refresh_canonical_timing(db, target_id)
            apply_rule(db, target_id, candidate["sport_type"], None)
            merged += 1
    return merged


def separate_same_connection_sources() -> int:
    """Split legacy canonical activities that contain multiple records from one connection.

    A provider connection's feed is a list of distinct rides.  Therefore it
    cannot supply two sources for one canonical activity, even if timestamps
    and distances happen to fall inside the cross-provider matching window.
    """
    separated = 0
    with connection() as db:
        duplicates = db.execute(
            """SELECT apl.activity_id, pa.connection_id
               FROM activity_provider_links apl
               JOIN provider_activities pa ON pa.id=apl.provider_activity_id
               GROUP BY apl.activity_id, pa.connection_id
               HAVING COUNT(*) > 1"""
        ).fetchall()
        for duplicate in duplicates:
            activity_id = duplicate["activity_id"]
            sources = db.execute(
                """SELECT pa.* FROM activity_provider_links apl
                   JOIN provider_activities pa ON pa.id=apl.provider_activity_id
                   WHERE apl.activity_id=? AND pa.connection_id=?
                   ORDER BY pa.started_at_epoch ASC, pa.id ASC""",
                (activity_id, duplicate["connection_id"]),
            ).fetchall()
            # Keep the earliest source on the existing canonical record; it
            # may already be corroborated by another provider. Every further
            # record becomes its own canonical activity and is rule-resolved.
            for source in sources[1:]:
                cursor = db.execute(
                    """INSERT INTO activities (public_id, name, sport_type, started_at, started_at_epoch, distance_m)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), source["name"] or "Unnamed activity", source["sport_type"],
                     source["started_at"], source["started_at_epoch"], source["distance_m"]),
                )
                split_activity_id = cursor.lastrowid
                db.execute("DELETE FROM activity_provider_links WHERE activity_id=? AND provider_activity_id=?", (activity_id, source["id"]))
                db.execute(
                    "INSERT INTO activity_provider_links (activity_id, provider_activity_id) VALUES (?, ?)",
                    (split_activity_id, source["id"]),
                )
                apply_rule(db, split_activity_id, source["sport_type"], None)
                separated += 1
            refresh_canonical_timing(db, activity_id)
    return separated


def sync_all_connections(
    errors: list[str] | None = None, progress: Callable[[str], None] | None = None,
) -> int:
    """Synchronize every active connection and optionally report human-readable progress."""
    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    total = 0
    with connection() as db:
        connections = db.execute(
            "SELECT * FROM provider_connections WHERE provider_type IN ('STRAVA_PROXY', 'HAMMERHEAD') AND status = 'CONNECTED'"
        ).fetchall()
    if not connections:
        report("No active provider connections found.")
    for index, provider_connection in enumerate(connections, start=1):
        label = provider_connection["display_name"] or provider_connection["external_account_id"] or provider_connection["provider_type"]
        report(f"Fetching activities from {label} ({index}/{len(connections)})…")
        try:
            imported = import_connection(provider_connection)
            total += imported
            report(f"{label}: activity feed processed; {imported} new activity{'ies' if imported != 1 else ''} imported.")
        except Exception as error:
            # A single unavailable account must not stop the other connections.
            report(f"{label}: unavailable; continuing with the remaining connections.")
            if errors is not None:
                account = provider_connection["external_account_id"]
                suffix = f" ({account})" if account else ""
                errors.append(f"{label}{suffix}: {error}")
            continue
    report("Reconciling provider records and applying rules…")
    repair_hammerhead_start_times()
    repair_strava_local_start_times()
    separate_same_connection_sources()
    refresh_all_canonical_timings()
    reconcile_single_source_activities()
    report("Activity records are up to date.")
    return total


def sync_loop() -> None:
    while not _stop.is_set():
        enabled, interval_seconds = scheduler_configuration()
        if enabled:
            sync_all_connections()
        _scheduler_wake.wait(interval_seconds)
        _scheduler_wake.clear()


def start_sync_loop() -> None:
    thread = threading.Thread(target=sync_loop, name="bike-garage-sync", daemon=True)
    thread.start()
