"""Backup, verify, migrate -- in that order, and refuse if the order breaks.

WHY THIS IS A MODULE AND NOT A RUNBOOK STEP

Opening a `HarnessStore` migrates the database. That is the correct behaviour
for a process starting up and a dangerous one for an operator at a prompt,
because by the time you have thought about a backup the migration has already
run. The only way to make "back up first" reliable is to make it the only
available path, so this module takes the backup, proves the copy is readable
and complete, and only then applies anything.

The backup is taken with SQLite's own `backup()` API rather than a file copy.
A copy of a live WAL database is not a database: the -wal file holds
committed pages the main file does not have yet, and `cp queue.db` on a busy
controller produces a file that opens fine and is missing the last minutes of
work. `backup()` walks the pages under a read lock and is the only way to get
a consistent snapshot without stopping the service.

ROLLBACK IS A FILE MOVE, AND THAT IS THE POINT

These migrations are additive -- `CREATE TABLE IF NOT EXISTS` and nothing
else -- so a rollback is never needed to recover DATA. It is needed to recover
a VERSION: an older binary reading a database stamped with a newer
user_version will refuse it. So rollback restores the snapshot wholesale,
which is correct precisely because the migration added tables the old code
does not know about and cannot have written to.
"""
from __future__ import annotations

import contextlib
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .schema import apply_migrations, get_schema_version


@contextlib.contextmanager
def _open(path: str | Path, *, read_only: bool = False):
    """A connection that is actually CLOSED afterwards.

    `with sqlite3.connect(...)` manages the transaction and not the handle --
    a genuine trap, and an expensive one here: a connection left open holds
    the -wal file, so a rollback that deletes it leaves the next read raising
    "disk I/O error" on a database that is in fact fine.
    """
    uri = f"file:{Path(path)}?mode=ro" if read_only else str(Path(path))
    connection = sqlite3.connect(uri, uri=read_only)
    try:
        yield connection
        if not read_only:
            connection.commit()
    finally:
        connection.close()


class MigrationRefused(RuntimeError):
    """The preconditions were not met. Nothing was changed."""


@dataclass
class MigrationReport:
    path: str
    backup_path: str | None
    version_before: int
    version_after: int
    applied: tuple[int, ...] = ()
    tables_added: tuple[str, ...] = ()
    row_counts_before: dict[str, int] = field(default_factory=dict)
    row_counts_after: dict[str, int] = field(default_factory=dict)
    integrity: str = ""
    dry_run: bool = False

    @property
    def rows_preserved(self) -> bool:
        """Every table that existed before still has exactly its rows.

        Checked rather than assumed. "Additive" is a property of the SQL as
        written; this is a property of what actually happened to this file.
        """
        return all(self.row_counts_after.get(name) == count
                   for name, count in self.row_counts_before.items())

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "backup_path": self.backup_path,
                "version_before": self.version_before,
                "version_after": self.version_after,
                "applied": list(self.applied),
                "tables_added": list(self.tables_added),
                "rows_preserved": self.rows_preserved,
                "row_counts_before": dict(self.row_counts_before),
                "row_counts_after": dict(self.row_counts_after),
                "integrity": self.integrity, "dry_run": self.dry_run}


def _tables(connection: sqlite3.Connection) -> list[str]:
    return sorted(row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts = {}
    for name in _tables(connection):
        try:
            counts[name] = connection.execute(f"SELECT COUNT(*) FROM \"{name}\"").fetchone()[0]
        except sqlite3.DatabaseError:
            counts[name] = -1
    return counts


def snapshot(path: str | Path, *, destination: str | Path | None = None) -> Path:
    """A consistent copy of a LIVE database. Never a file copy -- see above."""
    source = Path(path)
    if not source.is_file():
        raise MigrationRefused(f"no database at {source}")
    if destination is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = source.with_name(f"{source.name}.{stamp}.backup")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # sqlite3 will not write a backup into a file that is not a database,
        # and a half-written snapshot from a previous attempt is exactly that.
        target.unlink()
    live = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(target)
        try:
            live.backup(copy)
        finally:
            copy.close()
    finally:
        live.close()
    return target


def verify_snapshot(original: str | Path, backup: str | Path) -> dict[str, Any]:
    """Prove the copy is usable BEFORE anything is changed.

    Three questions, because each catches a different real failure: does it
    open and pass an integrity check (a torn copy), does it carry the same
    schema version (a copy of the wrong file), and does every table hold the
    same number of rows (a copy taken without the WAL).
    """
    with _open(original, read_only=True) as live, _open(backup, read_only=True) as copy:
        integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
        live_version = get_schema_version(live)
        copy_version = get_schema_version(copy)
        live_counts = _row_counts(live)
        copy_counts = _row_counts(copy)
    problems = []
    if integrity != "ok":
        problems.append(f"backup integrity_check says {integrity!r}")
    if live_version != copy_version:
        problems.append(f"backup is at user_version {copy_version}, source at {live_version}")
    for name, count in live_counts.items():
        if copy_counts.get(name) != count:
            problems.append(f"{name}: source has {count} rows, backup has {copy_counts.get(name)}")
    return {"ok": not problems, "problems": problems, "integrity": integrity,
            "version": copy_version, "row_counts": copy_counts}


def backup_then_migrate(path: str | Path, migrations: Sequence[Any], *,
                        backup_to: str | Path | None = None,
                        dry_run: bool = False) -> MigrationReport:
    """The only supported way to migrate a database that has data in it.

    `dry_run=True` takes and verifies the backup, reports exactly which
    migrations WOULD run, and changes nothing -- which is what you want
    against production before you want anything else.
    """
    target = Path(path)
    if not target.is_file():
        raise MigrationRefused(f"no database at {target}")

    backup = snapshot(target, destination=backup_to)
    verified = verify_snapshot(target, backup)
    if not verified["ok"]:
        raise MigrationRefused(
            "backup did not verify, so nothing was migrated: "
            + "; ".join(verified["problems"]))

    with _open(target) as connection:
        before_version = get_schema_version(connection)
        before_tables = set(_tables(connection))
        before_counts = _row_counts(connection)

    pending = tuple(sorted(m.version for m in migrations if m.version > before_version))
    if dry_run:
        return MigrationReport(
            path=str(target), backup_path=str(backup), version_before=before_version,
            version_after=before_version, applied=pending,
            row_counts_before=before_counts, row_counts_after=before_counts,
            integrity=verified["integrity"], dry_run=True)

    with _open(target) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        applied = apply_migrations(connection, list(migrations))

    with _open(target) as connection:
        after_version = get_schema_version(connection)
        after_tables = set(_tables(connection))
        after_counts = _row_counts(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]

    report = MigrationReport(
        path=str(target), backup_path=str(backup), version_before=before_version,
        version_after=after_version, applied=tuple(applied),
        tables_added=tuple(sorted(after_tables - before_tables)),
        row_counts_before=before_counts, row_counts_after=after_counts,
        integrity=integrity)
    if not report.rows_preserved:
        raise MigrationRefused(
            f"migration changed existing row counts; restore {backup} immediately. "
            f"before={before_counts} after={after_counts}")
    if integrity != "ok":
        raise MigrationRefused(
            f"integrity_check says {integrity!r} after migrating; restore {backup}")
    return report


def rollback(path: str | Path, backup: str | Path) -> dict[str, Any]:
    """Put the snapshot back, wholesale.

    Correct precisely BECAUSE the migration was additive: everything the new
    schema added is something the old code never wrote to, so there is no
    newer data in those tables to lose. The stale -wal/-shm are removed with
    it; leaving them beside a restored main file is how a "restored" database
    comes back up still carrying the pages that were rolled back.
    """
    target, source = Path(path), Path(backup)
    if not source.is_file():
        raise MigrationRefused(f"no backup at {source}")
    check = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        # A file that is not a database raises here rather than reporting a
        # bad integrity check, and an operator reaching for a rollback should
        # get one clear refusal either way -- not a DatabaseError traceback.
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationRefused(f"refusing to restore a corrupt backup: {source}")
        version = get_schema_version(check)
    except sqlite3.DatabaseError as exc:
        raise MigrationRefused(
            f"refusing to restore {source}: it is not a readable database ({exc})") from exc
    finally:
        check.close()
    for suffix in ("-wal", "-shm"):
        stale = Path(str(target) + suffix)
        if stale.exists():
            stale.unlink()
    shutil.copy2(source, target)
    return {"restored": str(target), "from": str(source), "user_version": version}
