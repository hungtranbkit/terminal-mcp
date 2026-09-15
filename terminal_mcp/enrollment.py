"""One-time node enrollment codes -- the credential a Windows (or any)
machine presents ONCE to trade for its real bootstrap configuration.

Why a separate credential at all, rather than shipping the node's bearer
token straight into the downloadable installer: the installer is a plain
.ps1 the operator saves to Downloads, mails to themselves, pastes into a
chat. Anything inside it is effectively public. So the file carries only
this code, which is

  - single-use   (consume() flips pending -> consumed ATOMICALLY, so a
                  replay of the same code loses the race and is refused,
                  even from two machines at the same instant),
  - short-lived  (default 15 minutes -- config.nodes.onboarding.
                  enrollment_ttl_seconds),
  - revocable    (revoke() from the dashboard, immediately),
  - hashed at rest (only sha256(code) is stored; a stolen enrollment.db
                  cannot be replayed against the controller).

The REAL secrets (the node's heartbeat bearer token, the rescue-tunnel
gateway credentials, a Tailscale auth key if one is configured) are
returned exactly once, over HTTPS/loopback, in the consume() response --
never written into the downloadable script, never logged, never returned
by any list/status route. See windows_onboarding.py for the script this
code is embedded in and dashboard.py's /dashboard/api/enroll/* routes for
the wire surface.

Same store discipline as connection_store.py/node_registry.py: sqlite
under ~/.local/state/terminal-mcp, 0600, WAL, PRAGMA user_version
migrations via schema.py.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

def _add_progress_columns(connection) -> None:
    for column, declaration in (("progress_stage", "TEXT"),
                                ("progress_at", "TEXT"),
                                ("progress_elapsed_seconds", "INTEGER")):
        connection.execute(f"ALTER TABLE enrollments ADD COLUMN {column} {declaration}")


def _add_handles(connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS enrollment_handles (
            handle_hash TEXT PRIMARY KEY,
            enrollment_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            redeemed_at TEXT,
            created_by TEXT
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_handles_enrollment "
                       "ON enrollment_handles(enrollment_id)")


ENROLLMENT_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: one-time node enrollment codes", lambda connection: None),
    Migration(2, "installer progress: which stage a machine is on, and for how long",
              _add_progress_columns),
    Migration(3, "bootstrap handles: a one-time id the web can hand a local helper",
              _add_handles),
]

# A handle is what travels in a terminalmcp:// URL, so it is sized for the
# gap between a click and a helper launching -- not for a human to type.
# How long a pairing handle stays usable. 120s was the original value and
# it was wrong by construction: the handle is minted when the DOWNLOAD
# starts, and the clock then has to cover the operator finding the file in
# Downloads, clearing a SmartScreen warning on an unsigned binary, and
# accepting a UAC prompt. Two minutes routinely expired before the helper
# ever ran, so a first-time install failed with no error anywhere -- the
# helper simply found a dead handle. 900s matches the enrollment TTL, so
# the pairing no longer dies before the thing it is paired with.
#
# Single-use and replay protection are unchanged: this widens the window
# in which ONE redemption may happen, never the number of redemptions.
HANDLE_TTL_SECONDS = 900
HANDLE_TTL_MIN_SECONDS = 30
HANDLE_TTL_MAX_SECONDS = 3600

# What the installer reports while it runs. A closed set: the progress
# route accepts nothing else, so a node cannot write arbitrary text into
# a field the dashboard renders.
# The helper's own stages, which happen BEFORE the PowerShell installer
# exists to report anything. Without these the Dashboard is blind from the
# moment the operator double-clicks until the script reaches 'starting' --
# which, if the helper dies on a dead handle or a blocked binary, is
# never. They are deliberately ordered: each is reported as it is entered.
STAGE_HELPER_STARTED = "helper_started"
STAGE_REDEEMING = "redeeming"
STAGE_REDEEMED = "redeemed"
STAGE_INSTALLING_SERVICE = "installing_service"
STAGE_LAUNCHING_SETUP = "launching_setup"
STAGE_SETUP_STARTED = "setup_started"

STAGE_STARTING = "starting"
STAGE_DOWNLOADING = "downloading"
STAGE_INSTALLING_OPENSSH = "installing_openssh"
STAGE_CONFIGURING_SSH = "configuring_ssh"
STAGE_REGISTERING = "registering"
STAGE_INSTALLING_TOOLS = "installing_tools"
STAGE_READY = "ready"
STAGE_FAILED = "failed"
HELPER_STAGES = (STAGE_HELPER_STARTED, STAGE_REDEEMING, STAGE_REDEEMED,
                 STAGE_INSTALLING_SERVICE, STAGE_LAUNCHING_SETUP, STAGE_SETUP_STARTED)

STAGES = HELPER_STAGES + (STAGE_STARTING, STAGE_DOWNLOADING, STAGE_INSTALLING_OPENSSH,
                          STAGE_CONFIGURING_SSH, STAGE_REGISTERING, STAGE_INSTALLING_TOOLS,
                          STAGE_READY, STAGE_FAILED)

# Human labels, kept next to the vocabulary so the dashboard and the
# installer cannot drift apart on what a stage is called.
STAGE_LABELS = {
    STAGE_HELPER_STARTED: "Helper đã khởi động",
    STAGE_REDEEMING: "Đang lấy cấu hình cài đặt",
    STAGE_REDEEMED: "Đã nhận cấu hình",
    STAGE_INSTALLING_SERVICE: "Đang cài dịch vụ nền",
    STAGE_LAUNCHING_SETUP: "Đang mở trình cài đặt",
    STAGE_SETUP_STARTED: "Trình cài đặt đã chạy",
    STAGE_STARTING: "Đang bắt đầu",
    STAGE_DOWNLOADING: "Đang tải bộ cài",
    STAGE_INSTALLING_OPENSSH: "Đang cài OpenSSH",
    STAGE_CONFIGURING_SSH: "Đang cấu hình SSH",
    STAGE_REGISTERING: "Đang đăng ký node",
    STAGE_INSTALLING_TOOLS: "Đang cài công cụ theo profile",
    STAGE_READY: "Hoàn tất",
    STAGE_FAILED: "Thất bại",
}

# The Bootstrap helper's own failure vocabulary, mirroring the closed set in
# helper/cmd/terminal-mcp-bootstrap/progress.go. A failed stage says only
# THAT it failed; these say which step, so "Cài đặt thất bại" stops being
# the end of the diagnosis. Closed on purpose: the helper must not be able
# to write arbitrary text into anything this controller logs or renders.
FAILURE_CODES = (
    "pairing_rejected",
    "controller_unreachable",
    "script_download_failed",
    "service_install_failed",
    "setup_launch_failed",
)

# Installer exit codes windows-setup.ps1 actually uses: 1 required steps
# failed, 2 ready-with-warnings, 3 elevation trouble, -1 the helper's own
# "no exit code" (timeout or could not start).
EXIT_CODE_MIN = -1
EXIT_CODE_MAX = 255


def normalize_failure_code(value: object) -> str | None:
    """A code from the closed set, or None. Never the caller's string."""
    candidate = str(value or "").strip()
    return candidate if candidate in FAILURE_CODES else None


def normalize_exit_code(value: object) -> int | None:
    """A bounded integer, or None. The machine reports this and it is only
    ever displayed or logged, so an unbounded value has no business here."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < EXIT_CODE_MIN or number > EXIT_CODE_MAX:
        return None
    return number


STATUS_PENDING = "pending"
STATUS_CONSUMED = "consumed"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"  # derived at read time, never persisted -- see _row_to_record

OS_WINDOWS = "windows"
OS_LINUX = "linux"
OS_MACOS = "macos"
SUPPORTED_OS = (OS_WINDOWS, OS_LINUX, OS_MACOS)

# Error codes returned by consume(). Deliberately distinct so the
# installer can print a remediation line per case instead of one generic
# "enrollment failed".
ERR_NOT_FOUND = "ENROLLMENT_NOT_FOUND"
ERR_EXPIRED = "ENROLLMENT_EXPIRED"
ERR_ALREADY_USED = "ENROLLMENT_ALREADY_USED"
ERR_REVOKED = "ENROLLMENT_REVOKED"

DEFAULT_TTL_SECONDS = 900  # 15 minutes -- the task's own "TTL ngắn khoảng 10-15 phút"

# Crockford-style base32 without I/L/O/U -- unambiguous when a human has
# to read a code off one screen and type it into another.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_GROUPS = 3
_GROUP_LEN = 5  # 15 chars * 5 bits = 75 bits of entropy
_CODE_RE = re.compile(r"^TMCP-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$")

_NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def default_enrollment_store_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_ENROLLMENT_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "enrollment.db"


def generate_code() -> str:
    """A fresh enrollment code. `secrets.choice` over the ambiguity-free
    alphabet above -- 75 bits, which is far past brute-forceable inside a
    15-minute window even with no rate limit in front of it (there is one
    anyway; see dashboard.py's enroll route)."""
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(_GROUP_LEN)) for _ in range(_GROUPS)]
    return "TMCP-" + "-".join(groups)


def normalize_code(value: str) -> str:
    """Accepts what a human actually types: lower case, missing dashes,
    surrounding whitespace, and the three characters Crockford maps back
    (I/L -> 1, O -> 0). Returns the canonical form, or "" if it cannot be
    read as a code at all -- the caller treats that as ENROLLMENT_NOT_FOUND
    rather than a distinct error, so probing for "is this shaped like a
    real code" tells an attacker nothing."""
    raw = re.sub(r"[\s-]", "", str(value or "")).upper()
    if raw.startswith("TMCP"):
        raw = raw[4:]
    raw = raw.replace("I", "1").replace("L", "1").replace("O", "0").replace("U", "V")
    if len(raw) != _GROUPS * _GROUP_LEN or not all(character in _ALPHABET for character in raw):
        return ""
    groups = [raw[index:index + _GROUP_LEN] for index in range(0, len(raw), _GROUP_LEN)]
    return "TMCP-" + "-".join(groups)


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def validate_node_id(value: str) -> str:
    """Node ids are lower-cased here (unlike remote_connect.validate_node_id,
    which preserves case) because a node id becomes part of an env var
    name, a Windows Scheduled Task name and a gateway authorized_keys
    comment -- one canonical spelling avoids "M910" and "m910" enrolling
    as two nodes that are really one machine."""
    candidate = str(value or "").strip().lower()
    if not _NODE_ID_RE.match(candidate):
        raise ValueError("node_id must be 1-64 chars of a-z, 0-9, '-' or '_' and start with a letter or digit")
    if candidate == "local":
        raise ValueError("node_id 'local' is reserved for the controller's own node")
    return candidate


def suggest_node_id(hostname: str) -> str:
    """Best-effort hostname -> node id. Never raises: an unusable hostname
    falls back to a random suffix rather than blocking the form."""
    candidate = re.sub(r"[^a-z0-9_-]+", "-", str(hostname or "").strip().lower()).strip("-_")
    candidate = re.sub(r"-{2,}", "-", candidate)[:64]
    if not candidate or not _NODE_ID_RE.match(candidate) or candidate == "local":
        candidate = f"node-{secrets.token_hex(3)}"
    return candidate


@dataclass(frozen=True)
class Enrollment:
    """One enrollment row, WITHOUT the code or its hash -- neither is ever
    part of this dataclass, so no list/status/audit path can leak one by
    accident. `code_display` is the first group only (e.g. "TMCP-4KQ7M…"),
    enough for an operator to tell two pending codes apart."""
    id: str
    node_id: str
    display_name: str
    os: str
    profile: str
    connectivity: dict[str, Any]
    status: str
    code_display: str
    created_at: str
    expires_at: str
    created_by: str | None
    consumed_at: str | None
    consumed_from: str | None
    consumed_hostname: str | None
    revoked_at: str | None
    revoked_by: str | None
    progress_stage: str | None = None
    progress_at: str | None = None
    progress_elapsed_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "node_id": self.node_id, "display_name": self.display_name,
            "os": self.os, "profile": self.profile, "connectivity": dict(self.connectivity),
            "status": self.status, "code_display": self.code_display,
            "created_at": self.created_at, "expires_at": self.expires_at,
            "created_by": self.created_by, "consumed_at": self.consumed_at,
            "consumed_from": self.consumed_from, "consumed_hostname": self.consumed_hostname,
            "revoked_at": self.revoked_at, "revoked_by": self.revoked_by,
            "progress_stage": self.progress_stage,
            "progress_label": STAGE_LABELS.get(self.progress_stage or "", None),
            "progress_at": self.progress_at,
            "progress_elapsed_seconds": self.progress_elapsed_seconds,
        }


class EnrollmentStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_enrollment_store_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS enrollments (
                    id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL UNIQUE,
                    code_display TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    os TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    connectivity TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_by TEXT,
                    consumed_at TEXT,
                    consumed_from TEXT,
                    consumed_hostname TEXT,
                    revoked_at TEXT,
                    revoked_by TEXT
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_enrollments_status ON enrollments(status, expires_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_enrollments_node ON enrollments(node_id)")
            apply_migrations(connection, ENROLLMENT_MIGRATIONS)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    # -- create ------------------------------------------------------------

    def create(self, *, node_id: str, display_name: str | None = None, os_name: str = OS_WINDOWS,
               profile: str = "minimal", connectivity: dict[str, Any] | None = None,
               ttl_seconds: int = DEFAULT_TTL_SECONDS, created_by: str | None = None,
               now: datetime | None = None) -> tuple[Enrollment, str]:
        """Returns (record, plaintext_code). The plaintext is returned HERE
        and nowhere else, ever -- the caller embeds it in the one download
        it hands the operator and then drops it."""
        node_id = validate_node_id(node_id)
        if os_name not in SUPPORTED_OS:
            raise ValueError(f"os must be one of {', '.join(SUPPORTED_OS)}")
        if ttl_seconds < 60 or ttl_seconds > 86400:
            raise ValueError("ttl_seconds must be between 60 and 86400")
        moment = now or _now()
        code = generate_code()
        record_id = secrets.token_hex(8)
        payload = json.dumps(connectivity or {}, sort_keys=True)
        row = {
            "id": record_id, "code_hash": hash_code(code), "code_display": code.split("-")[1],
            "node_id": node_id, "display_name": (display_name or node_id).strip() or node_id,
            "os": os_name, "profile": profile, "connectivity": payload, "status": STATUS_PENDING,
            "created_at": _iso(moment), "expires_at": _iso(moment + timedelta(seconds=ttl_seconds)),
            "created_by": created_by,
        }
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO enrollments (id, code_hash, code_display, node_id, display_name, os, profile,
                                            connectivity, status, created_at, expires_at, created_by)
                   VALUES (:id, :code_hash, :code_display, :node_id, :display_name, :os, :profile,
                           :connectivity, :status, :created_at, :expires_at, :created_by)""",
                row,
            )
        record = self.get(record_id, now=moment)
        assert record is not None  # just inserted
        return record, code

    # -- consume -----------------------------------------------------------

    def consume(self, code: str, *, hostname: str | None = None, source_ip: str | None = None,
                now: datetime | None = None) -> tuple[Enrollment | None, str | None]:
        """Single-use, atomic. Returns (record, None) on success or
        (None, error_code) otherwise.

        The state flip is ONE `UPDATE ... WHERE status='pending' AND
        expires_at > now` statement: whichever caller's UPDATE commits
        first gets rowcount 1, every concurrent replay gets 0 and is
        refused. There is deliberately no read-then-write window here --
        that window is exactly how a single-use token becomes double-use
        under two simultaneous installers."""
        moment = now or _now()
        canonical = normalize_code(code)
        if not canonical:
            return None, ERR_NOT_FOUND
        digest = hash_code(canonical)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE enrollments
                          SET status = ?, consumed_at = ?, consumed_from = ?, consumed_hostname = ?
                        WHERE code_hash = ? AND status = ? AND expires_at > ?""",
                    (STATUS_CONSUMED, _iso(moment), source_ip, (hostname or None), digest,
                     STATUS_PENDING, _iso(moment)),
                )
                if cursor.rowcount == 1:
                    row = connection.execute("SELECT * FROM enrollments WHERE code_hash = ?", (digest,)).fetchone()
                    connection.execute("COMMIT")
                    return _row_to_record(row, now=moment), None
                row = connection.execute("SELECT * FROM enrollments WHERE code_hash = ?", (digest,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if row is None:
            return None, ERR_NOT_FOUND
        if row["status"] == STATUS_CONSUMED:
            return None, ERR_ALREADY_USED
        if row["status"] == STATUS_REVOKED:
            return None, ERR_REVOKED
        return None, ERR_EXPIRED

    # -- read / manage -----------------------------------------------------

    def get(self, enrollment_id: str, *, now: datetime | None = None) -> Enrollment | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
        return _row_to_record(row, now=now or _now())

    def list(self, *, node_id: str | None = None, include_terminal: bool = True,
             limit: int = 100, now: datetime | None = None) -> list[Enrollment]:
        moment = now or _now()
        query = "SELECT * FROM enrollments"
        params: list[Any] = []
        if node_id:
            query += " WHERE node_id = ?"
            params.append(node_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        records = [record for row in rows if (record := _row_to_record(row, now=moment)) is not None]
        if include_terminal:
            return records
        return [record for record in records if record.status == STATUS_PENDING]

    def revoke(self, enrollment_id: str, *, by: str | None = None, now: datetime | None = None) -> bool:
        """Revoking a pending code kills it immediately. Revoking an
        already-consumed one is a no-op here on purpose: the node's real
        credentials are what has to be revoked at that point, which is the
        Remove Node path (dashboard.py), not this."""
        moment = now or _now()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE enrollments SET status = ?, revoked_at = ?, revoked_by = ? WHERE id = ? AND status = ?",
                (STATUS_REVOKED, _iso(moment), by, enrollment_id, STATUS_PENDING),
            )
        return cursor.rowcount == 1

    def revoke_pending_for_node(self, node_id: str, *, by: str | None = None,
                                now: datetime | None = None) -> int:
        moment = now or _now()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE enrollments SET status = ?, revoked_at = ?, revoked_by = ? WHERE node_id = ? AND status = ?",
                (STATUS_REVOKED, _iso(moment), by, node_id, STATUS_PENDING),
            )
        return cursor.rowcount

    def purge(self, *, older_than_days: int = 30, now: datetime | None = None) -> int:
        cutoff = (now or _now()) - timedelta(days=max(1, older_than_days))
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM enrollments WHERE created_at < ?", (_iso(cutoff),))
        return cursor.rowcount

    def record_progress_by_handle(self, handle: str, *, stage: str,
                                  elapsed_seconds: int | None = None,
                                  now: datetime | None = None) -> "Enrollment | None":
        """The same telemetry, authenticated by the PAIRING HANDLE instead
        of the enrollment code.

        This exists because the helper does not hold the code. It holds a
        handle, and the most useful things it can tell us -- "I started",
        "I am about to redeem", "redeeming failed" -- all happen BEFORE
        the exchange that would give it a code. Reporting them was
        impossible, so the Dashboard was blind for exactly the window in
        which first-time installs fail.

        Deliberately NOT single-use and deliberately tolerant of a handle
        that has already been redeemed: redemption is the one-shot state
        change, and the helper keeps narrating afterwards (installing the
        service, launching setup). Widening this to progress would mean
        the helper could report its first stage and then go silent.

        Still fail-closed on everything that matters: an unparseable
        handle, an unknown handle, or an enrollment that has expired or
        been revoked all return None and are answered identically by the
        route. A handle is 128 bits of entropy and never appears in a log.
        """
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        candidate = str(handle or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", candidate):
            return None
        moment = now or _now()
        digest = hash_code(candidate)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT enrollment_id FROM enrollment_handles WHERE handle_hash = ?",
                (digest,)).fetchone()
        if not row:
            return None
        record = self.get(row["enrollment_id"], now=moment)
        # An enrollment that is expired or revoked stops accepting
        # narration: it cannot complete, so progress against it would only
        # ever be a misleading spinner on somebody's dashboard.
        if record is None or record.status in (STATUS_REVOKED, STATUS_EXPIRED):
            return None
        return self._write_progress(record.id, stage=stage, elapsed_seconds=elapsed_seconds,
                                    now=moment)

    def _write_progress(self, enrollment_id: str, *, stage: str,
                        elapsed_seconds: int | None, now: datetime) -> "Enrollment | None":
        """The one UPDATE both progress paths share, so code-authenticated
        and handle-authenticated telemetry can never drift on clamping or
        on which columns move."""
        elapsed = None if elapsed_seconds is None else max(0, min(int(elapsed_seconds), 86_400))
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE enrollments SET progress_stage = ?, progress_at = ?, progress_elapsed_seconds = ? "
                "WHERE id = ?",
                (stage, _iso(now), elapsed, enrollment_id))
            if cursor.rowcount != 1:
                return None
        return self.get(enrollment_id, now=now)

    def record_progress(self, code: str, *, stage: str, elapsed_seconds: int | None = None,
                        now: datetime | None = None) -> Enrollment | None:
        """Best-effort telemetry from a machine that is mid-install.

        Authenticated by the enrollment code, which the installer already
        holds -- and deliberately does NOT consume it: progress arrives
        both before the exchange (while OpenSSH installs) and after it
        (while winget runs), and a single mechanism for both beats two.

        `stage` must be one of STAGES; anything else is refused rather
        than stored, because this value is rendered on the dashboard.
        Returns None when the code is unknown, so the caller can answer
        identically to a wrong code and leak nothing."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        canonical = normalize_code(code)
        if not canonical:
            return None
        moment = now or _now()
        # Clamp: this number is reported by the machine and only ever
        # displayed, but an unbounded value has no business in the store.
        elapsed = None if elapsed_seconds is None else max(0, min(int(elapsed_seconds), 86_400))
        digest = hash_code(canonical)
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE enrollments SET progress_stage = ?, progress_at = ?, progress_elapsed_seconds = ? "
                "WHERE code_hash = ?",
                (stage, _iso(moment), elapsed, digest))
            if cursor.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM enrollments WHERE code_hash = ?", (digest,)).fetchone()
        return _row_to_record(row, now=moment)

    # -- bootstrap handles ---------------------------------------------
    # A custom-protocol URL is not a private channel: it can reach the
    # registry's MRU, browser history, a crash report. So the URL carries
    # one of these instead of the enrollment code -- 128 bits, hashed at
    # rest, single-use, and alive for two minutes.

    def create_handle(self, enrollment_id: str, *, created_by: str | None = None,
                      ttl_seconds: int = HANDLE_TTL_SECONDS,
                      now: datetime | None = None) -> tuple[str, str] | None:
        """Returns (handle, expires_at) once, or None if the enrollment is
        not in a state worth handing to a helper."""
        moment = now or _now()
        record = self.get(enrollment_id, now=moment)
        if record is None or record.status != STATUS_PENDING:
            return None
        handle = secrets.token_hex(16)
        expires = _iso(moment + timedelta(seconds=max(HANDLE_TTL_MIN_SECONDS,
                                                     min(int(ttl_seconds), HANDLE_TTL_MAX_SECONDS))))
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO enrollment_handles (handle_hash, enrollment_id, created_at, expires_at, created_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (hash_code(handle), enrollment_id, _iso(moment), expires, created_by))
        return handle, expires

    def redeem_handle(self, handle: str, *, now: datetime | None = None) -> Enrollment | None:
        """Single-use, atomic, same discipline as consume(): the UPDATE
        that marks it redeemed is the guard, so two helpers racing the
        same handle produce exactly one winner. Returns the enrollment the
        handle points at -- the CALLER decides what to hand over."""
        moment = now or _now()
        candidate = str(handle or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", candidate):
            return None
        digest = hash_code(candidate)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE enrollment_handles SET redeemed_at = ? "
                    "WHERE handle_hash = ? AND redeemed_at IS NULL AND expires_at > ?",
                    (_iso(moment), digest, _iso(moment)))
                if cursor.rowcount != 1:
                    connection.execute("COMMIT")
                    return None
                row = connection.execute(
                    "SELECT enrollment_id FROM enrollment_handles WHERE handle_hash = ?", (digest,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
        return self.get(row["enrollment_id"], now=moment) if row else None

    def consume_by_id(self, enrollment_id: str, *, hostname: str | None = None,
                      source_ip: str | None = None,
                      now: datetime | None = None) -> tuple[Enrollment | None, str | None]:
        """consume(), keyed on the record instead of the code.

        This is what the helper path uses, and it is the reason the
        enrollment code never leaves the server for that path at all: the
        store keeps only sha256(code) and genuinely cannot reproduce one,
        so redeeming a handle performs the consume here rather than
        handing a credential back to the machine to replay.

        Same atomic single-UPDATE guard as consume(): two helpers racing
        one handle already resolve to one winner, and this closes the
        second door behind them."""
        moment = now or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE enrollments
                          SET status = ?, consumed_at = ?, consumed_from = ?, consumed_hostname = ?
                        WHERE id = ? AND status = ? AND expires_at > ?""",
                    (STATUS_CONSUMED, _iso(moment), source_ip, (hostname or None), enrollment_id,
                     STATUS_PENDING, _iso(moment)))
                row = connection.execute("SELECT * FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
                connection.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
        if cursor.rowcount == 1:
            return _row_to_record(row, now=moment), None
        if row is None:
            return None, ERR_NOT_FOUND
        if row["status"] == STATUS_CONSUMED:
            return None, ERR_ALREADY_USED
        if row["status"] == STATUS_REVOKED:
            return None, ERR_REVOKED
        return None, ERR_EXPIRED

    def verify_code_matches(self, enrollment_id: str, code: str) -> bool:
        """Constant-time check used only by tests and by the repair path,
        where the caller already holds a record id."""
        canonical = normalize_code(code)
        if not canonical:
            return False
        with self._connection() as connection:
            row = connection.execute("SELECT code_hash FROM enrollments WHERE id = ?", (enrollment_id,)).fetchone()
        if row is None:
            return False
        return hmac.compare_digest(row["code_hash"], hash_code(canonical))


def _row_to_record(row: sqlite3.Row | None, *, now: datetime) -> Enrollment | None:
    if row is None:
        return None
    status = row["status"]
    if status == STATUS_PENDING:
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            expires = now
        if expires <= now:
            status = STATUS_EXPIRED
    try:
        connectivity = json.loads(row["connectivity"] or "{}")
    except (TypeError, ValueError):
        connectivity = {}
    return Enrollment(
        id=row["id"], node_id=row["node_id"], display_name=row["display_name"], os=row["os"],
        profile=row["profile"], connectivity=connectivity if isinstance(connectivity, dict) else {},
        status=status, code_display=f"TMCP-{row['code_display']}-…",
        created_at=row["created_at"], expires_at=row["expires_at"], created_by=row["created_by"],
        consumed_at=row["consumed_at"], consumed_from=row["consumed_from"],
        consumed_hostname=row["consumed_hostname"], revoked_at=row["revoked_at"], revoked_by=row["revoked_by"],
        progress_stage=_column(row, "progress_stage"),
        progress_at=_column(row, "progress_at"),
        progress_elapsed_seconds=_column(row, "progress_elapsed_seconds"),
    )


def _column(row: sqlite3.Row, name: str):
    """Reads a column that may predate this store's migration 2 -- a row
    fetched before the ALTER has no such key at all."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None
