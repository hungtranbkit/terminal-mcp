"""Real SQLite schema version tracking -- P1 hardening item #10.

This project's existing migrations (the "ALTER TABLE ADD COLUMN IF NOT
EXISTS on every startup" pattern already used throughout audit.py/
bindings.py/supervisor.py/supervisor2.py) are individually safe and
already proven correct in production across this session's own P0/P0.5
hardening phases; replacing that pattern wholesale was judged higher risk
for marginal benefit in a single-operator, single-file-per-store
deployment with no rollback or multiple-schema-version-in-flight
requirement. What was genuinely missing: any durable, queryable record of
which schema state a database file is actually in, and a real mechanism
to apply FUTURE migrations in order, exactly once, tracked -- rather than
purely "columns get added if they happen to be absent" with no audit
trail of what ran or when.

Uses SQLite's own PRAGMA user_version (a plain integer stored in the
database file's header -- free, transactional, no extra table, survives
VACUUM/backup/copy) as the version counter. Each store stamps its
existing (already-migrated, by the pre-existing ad-hoc pattern) schema as
a numbered baseline the first time it opens under this system; any new
schema change from this point on is a real, ordered, tracked Migration
appended to that store's list instead of another bare ALTER TABLE.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def get_schema_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def apply_migrations(connection: sqlite3.Connection, migrations: list[Migration]) -> list[int]:
    """Applies every migration whose version is greater than the db's
    current PRAGMA user_version, in ascending version order, each inside
    its own transaction -- user_version is bumped to that migration's
    version immediately after it succeeds, so a crash mid-migration never
    leaves the counter claiming a migration completed that didn't, and a
    restart safely resumes from the last one that actually committed.
    Returns the list of versions actually applied (empty if the db was
    already current -- the overwhelmingly common case on every ordinary
    startup once a db has been migrated to the latest version once)."""
    applied = []
    for migration in sorted(migrations, key=lambda m: m.version):
        if migration.version <= get_schema_version(connection):
            continue
        with connection:
            migration.apply(connection)
            # PRAGMA does not accept bound parameters -- the version is
            # this module's own int, never user input, so this is safe.
            connection.execute(f"PRAGMA user_version = {migration.version}")
        applied.append(migration.version)
    return applied
