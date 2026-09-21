from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/bike-garage.db"))


def initialise_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        # V1 called these "count rules". A rule is now the single resolver
        # primitive and its source is simply an expression. Rename in place so
        # local installations keep both rules and their run history intact.
        tables = {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "resolver_count_rules" in tables and "resolver_rules" not in tables:
            db.execute("ALTER TABLE resolver_count_rules RENAME TO resolver_rules")
        if "resolver_rules" in tables or "resolver_count_rules" in tables:
            rule_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(resolver_rules)")
            }
            if "count_expression" in rule_columns and "expression" not in rule_columns:
                db.execute("ALTER TABLE resolver_rules RENAME COLUMN count_expression TO expression")
        db.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                login_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS passkeys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                credential_id TEXT NOT NULL UNIQUE,
                credential_public_key BLOB NOT NULL,
                sign_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                mileage_tracking_started_at TEXT,
                mileage_tracking_started_at_epoch INTEGER,
                scheduler_enabled INTEGER NOT NULL DEFAULT 1,
                scheduler_interval_seconds INTEGER,
                strava_auto_update_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bikes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                identifier TEXT NOT NULL DEFAULT '',
                bike_type TEXT NOT NULL,
                owner_name TEXT NOT NULL,
                frame_number TEXT,
                details_markdown TEXT NOT NULL DEFAULT '',
                photo_filename TEXT NOT NULL,
                starting_mileage_m REAL NOT NULL DEFAULT 0,
                colour TEXT NOT NULL DEFAULT '#2f765b',
                strava_activity_type TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS provider_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                provider_type TEXT NOT NULL CHECK (provider_type IN ('STRAVA', 'STRAVA_PROXY', 'HAMMERHEAD')),
                display_name TEXT NOT NULL,
                identifier TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN ('CONNECTED', 'NEEDS_CONFIGURATION', 'DISCONNECTED')),
                endpoint_url TEXT,
                access_token TEXT,
                external_account_id TEXT,
                oauth_client_id TEXT,
                oauth_client_secret TEXT,
                refresh_token TEXT,
                token_expires_at INTEGER,
                oauth_state TEXT,
                activity_types_json TEXT,
                last_tested_at TEXT,
                last_test_status TEXT,
                last_test_message TEXT,
                last_test_activity_count INTEGER,
                last_test_activities_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS provider_activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id INTEGER NOT NULL REFERENCES provider_connections(id),
                external_activity_id TEXT NOT NULL,
                name TEXT NOT NULL,
                sport_type TEXT,
                started_at TEXT,
                started_at_epoch INTEGER,
                distance_m REAL NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (connection_id, external_activity_id)
            );

            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                manual_title TEXT,
                sport_type TEXT,
                started_at TEXT,
                started_at_epoch INTEGER,
                distance_m REAL NOT NULL DEFAULT 0,
                expected_bike_count INTEGER NOT NULL DEFAULT 1,
                rule_hash TEXT,
                activity_hash TEXT,
                deleted_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS resolver_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                sport_type TEXT,
                provider_account_id TEXT,
                bike_count INTEGER NOT NULL CHECK (bike_count >= 1),
                expression TEXT NOT NULL DEFAULT 'linked_provider_activities',
                condition_operator TEXT NOT NULL DEFAULT '>=',
                condition_value INTEGER NOT NULL DEFAULT 1,
                is_catch_all INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS activity_provider_links (
                activity_id INTEGER NOT NULL REFERENCES activities(id),
                provider_activity_id INTEGER NOT NULL REFERENCES provider_activities(id),
                PRIMARY KEY (activity_id, provider_activity_id)
            );

            CREATE TABLE IF NOT EXISTS activity_bike_assignments (
                activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
                bike_id INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
                slot_index INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'MANUAL',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (activity_id, slot_index),
                UNIQUE (activity_id, bike_id, slot_index)
            );

            CREATE TABLE IF NOT EXISTS activity_rule_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
                resolution_id TEXT NOT NULL,
                rule_id INTEGER NOT NULL REFERENCES resolver_rules(id) ON DELETE CASCADE,
                rule_name TEXT NOT NULL,
                result_text TEXT NOT NULL,
                log_output_json TEXT NOT NULL DEFAULT '[]',
                provider_snapshot_json TEXT NOT NULL DEFAULT '[]',
                applied INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_activity_rule_runs_activity_resolution
                ON activity_rule_runs(activity_id, resolution_id, id DESC);

            CREATE TABLE IF NOT EXISTS activity_log_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
                logger TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_activity_log_entries_activity_created
                ON activity_log_entries(activity_id, id DESC);

            CREATE TABLE IF NOT EXISTS bike_components (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bike_id INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
                component_type TEXT NOT NULL CHECK (component_type IN ('shifting', 'bike_power', 'seatpost')),
                ant_device_number INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (bike_id, component_type)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_bike_components_ant_identity
                ON bike_components(component_type, ant_device_number)
                WHERE ant_device_number IS NOT NULL;

            CREATE TABLE IF NOT EXISTS strava_gears (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_connection_id INTEGER NOT NULL REFERENCES provider_connections(id) ON DELETE CASCADE,
                external_gear_id TEXT NOT NULL,
                name TEXT NOT NULL,
                distance_m REAL,
                is_primary INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT '{}',
                synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (provider_connection_id, external_gear_id)
            );
            CREATE INDEX IF NOT EXISTS idx_strava_gears_connection
                ON strava_gears(provider_connection_id, name COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS bike_strava_gear_mappings (
                bike_id INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
                strava_gear_id INTEGER NOT NULL REFERENCES strava_gears(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (bike_id, strava_gear_id),
                UNIQUE (strava_gear_id)
            );
            CREATE INDEX IF NOT EXISTS idx_bike_strava_gears_bike
                ON bike_strava_gear_mappings(bike_id);

            CREATE TABLE IF NOT EXISTS provider_activity_hardware (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_activity_id INTEGER NOT NULL REFERENCES provider_activities(id) ON DELETE CASCADE,
                component_type TEXT NOT NULL,
                protocol TEXT NOT NULL DEFAULT 'ANT_PLUS',
                ant_device_number INTEGER,
                manufacturer_id INTEGER,
                manufacturer_name TEXT,
                device_type_id INTEGER,
                serial_number INTEGER,
                product_id INTEGER,
                product_name TEXT,
                battery_status TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                observed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (provider_activity_id, component_type, ant_device_number)
            );
            CREATE INDEX IF NOT EXISTS idx_provider_activity_hardware_lookup
                ON provider_activity_hardware(provider_activity_id, component_type, ant_device_number);
            """
        )
        existing_columns = {row["name"] for row in db.execute("PRAGMA table_info(provider_connections)")}
        user_settings_columns = {row["name"] for row in db.execute("PRAGMA table_info(user_settings)")}
        if "scheduler_enabled" not in user_settings_columns:
            db.execute("ALTER TABLE user_settings ADD COLUMN scheduler_enabled INTEGER NOT NULL DEFAULT 1")
        if "scheduler_interval_seconds" not in user_settings_columns:
            db.execute("ALTER TABLE user_settings ADD COLUMN scheduler_interval_seconds INTEGER")
        if "strava_auto_update_enabled" not in user_settings_columns:
            db.execute("ALTER TABLE user_settings ADD COLUMN strava_auto_update_enabled INTEGER NOT NULL DEFAULT 0")
        for name, definition in (
            ("identifier", "TEXT NOT NULL DEFAULT ''"),
            ("last_tested_at", "TEXT"),
            ("last_test_status", "TEXT"),
            ("last_test_message", "TEXT"),
            ("last_test_activity_count", "INTEGER"),
            ("last_test_activities_json", "TEXT"),
            ("activity_types_json", "TEXT"),
            ("oauth_client_id", "TEXT"),
            ("oauth_client_secret", "TEXT"),
            ("refresh_token", "TEXT"),
            ("token_expires_at", "INTEGER"),
            ("oauth_state", "TEXT"),
        ):
            if name not in existing_columns:
                db.execute(f"ALTER TABLE provider_connections ADD COLUMN {name} {definition}")
        # Provider-side account IDs are not a good rule-facing identity: an
        # OAuth provider can choose them and they may change. Backfill a local,
        # editable connection identifier for prototype databases created before
        # the field existed.
        used_identifiers = {
            row["identifier"] for row in db.execute(
                "SELECT identifier FROM provider_connections WHERE identifier <> ''"
            ).fetchall()
        }
        for provider_connection in db.execute(
            "SELECT id, display_name, provider_type FROM provider_connections WHERE identifier = ''"
        ).fetchall():
            base = re.sub(r"[^a-z0-9]+", "-", provider_connection["display_name"].lower()).strip("-")
            base = base or provider_connection["provider_type"].lower().replace("_", "-")
            identifier = base
            suffix = 2
            while identifier in used_identifiers:
                identifier = f"{base}-{suffix}"
                suffix += 1
            used_identifiers.add(identifier)
            db.execute("UPDATE provider_connections SET identifier = ? WHERE id = ?", (identifier, provider_connection["id"]))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_connections_identifier ON provider_connections(identifier) WHERE identifier <> ''")
        activity_columns = {row["name"] for row in db.execute("PRAGMA table_info(activities)")}
        bike_columns = {row["name"] for row in db.execute("PRAGMA table_info(bikes)")}
        rule_run_columns = {row["name"] for row in db.execute("PRAGMA table_info(activity_rule_runs)")}
        if "provider_snapshot_json" not in rule_run_columns:
            db.execute("ALTER TABLE activity_rule_runs ADD COLUMN provider_snapshot_json TEXT NOT NULL DEFAULT '[]'")
        if "identifier" not in bike_columns:
            db.execute("ALTER TABLE bikes ADD COLUMN identifier TEXT NOT NULL DEFAULT ''")
            for bike in db.execute("SELECT id, name FROM bikes WHERE identifier = ''").fetchall():
                db.execute("UPDATE bikes SET identifier=? WHERE id=?", (f"bike-{bike['id']}", bike["id"]))
        if "starting_mileage_m" not in bike_columns:
            db.execute("ALTER TABLE bikes ADD COLUMN starting_mileage_m REAL NOT NULL DEFAULT 0")
        if "colour" not in bike_columns:
            db.execute("ALTER TABLE bikes ADD COLUMN colour TEXT NOT NULL DEFAULT '#2f765b'")
        if "frame_number" not in bike_columns:
            db.execute("ALTER TABLE bikes ADD COLUMN frame_number TEXT")
        if "details_markdown" not in bike_columns:
            db.execute("ALTER TABLE bikes ADD COLUMN details_markdown TEXT NOT NULL DEFAULT ''")
        if "strava_activity_type" not in bike_columns:
            db.execute("ALTER TABLE bikes ADD COLUMN strava_activity_type TEXT")
        if "expected_bike_count" not in activity_columns:
            db.execute("ALTER TABLE activities ADD COLUMN expected_bike_count INTEGER NOT NULL DEFAULT 1")
        if "rule_hash" not in activity_columns:
            db.execute("ALTER TABLE activities ADD COLUMN rule_hash TEXT")
        if "activity_hash" not in activity_columns:
            db.execute("ALTER TABLE activities ADD COLUMN activity_hash TEXT")
        if "manual_title" not in activity_columns:
            db.execute("ALTER TABLE activities ADD COLUMN manual_title TEXT")
        if "deleted_at" not in activity_columns:
            db.execute("ALTER TABLE activities ADD COLUMN deleted_at TEXT")
        if "public_id" not in activity_columns:
            db.execute("ALTER TABLE activities ADD COLUMN public_id TEXT")
        for activity in db.execute(
            "SELECT id FROM activities WHERE public_id IS NULL OR public_id = ''"
        ).fetchall():
            db.execute(
                "UPDATE activities SET public_id=? WHERE id=?",
                (str(uuid.uuid4()), activity["id"]),
            )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_public_id "
            "ON activities(public_id) WHERE public_id IS NOT NULL AND public_id <> ''"
        )
        # A former on-demand Hammerhead detail refresh accidentally replaced
        # imported_at. FIT observations are saved during the original import,
        # so they provide the trustworthy first-import timestamp for affected
        # historical records.
        db.execute(
            """UPDATE provider_activities AS activity
               SET imported_at = (
                   SELECT MIN(hardware.created_at)
                   FROM provider_activity_hardware AS hardware
                   WHERE hardware.provider_activity_id = activity.id
               )
               WHERE EXISTS (
                   SELECT 1 FROM provider_connections AS provider
                   WHERE provider.id = activity.connection_id
                     AND provider.provider_type = 'HAMMERHEAD'
               )
                 AND EXISTS (
                   SELECT 1 FROM provider_activity_hardware AS hardware
                   WHERE hardware.provider_activity_id = activity.id
                   GROUP BY hardware.provider_activity_id
                   HAVING MIN(hardware.created_at) < activity.imported_at
               )"""
        )
        rule_columns = {row["name"] for row in db.execute("PRAGMA table_info(resolver_rules)")}
        if "is_catch_all" not in rule_columns:
            db.execute("ALTER TABLE resolver_rules ADD COLUMN is_catch_all INTEGER NOT NULL DEFAULT 0")
        if "expression" not in rule_columns:
            db.execute("ALTER TABLE resolver_rules ADD COLUMN expression TEXT NOT NULL DEFAULT 'linked_provider_activities'")
        if "condition_operator" not in rule_columns:
            db.execute("ALTER TABLE resolver_rules ADD COLUMN condition_operator TEXT NOT NULL DEFAULT '>='")
        if "condition_value" not in rule_columns:
            db.execute("ALTER TABLE resolver_rules ADD COLUMN condition_value INTEGER NOT NULL DEFAULT 1")
        existing = db.execute("SELECT id FROM users LIMIT 1").fetchone()
        if existing is None:
            db.execute(
                "INSERT INTO users (display_name, login_enabled) VALUES (?, ?)",
                ("Jakez", 1),
            )
        # Upgrade the original prototype's seeded display name in existing local databases.
        db.execute("UPDATE users SET display_name = ? WHERE display_name = ?", ("Jakez", "Jake" + "s"))


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def initial_user() -> sqlite3.Row:
    with connection() as db:
        return db.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
