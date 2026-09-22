"""Disposable-tmux-session isolation for the suites that create REAL
tmux sessions (task blg_8a1389e7caeb).

The bug this exists to remove, reproduced exactly before it was written:
`test_kill_reopen.py` used FIXED session names (`lifecycle-smoke-kill-1`,
`lifecycle-shell-only`, ...). One interrupted or failing run leaves such
a session behind, and from then on EVERY later run of that test fails
with `SESSION_ALREADY_EXISTS` -- the failure is self-perpetuating,
because the test that leaked is the same test that then fails before
reaching its own cleanup line. That is a test-isolation defect, not a
product defect: `SESSION_ALREADY_EXISTS` is the CORRECT product answer
when a session by that name really does exist.

Three things fix it, and all three are needed:

1. **Unique names.** Every session a test creates is named for THIS run,
   so a leftover from any other run (or another lane running the same
   suite at the same time) can never collide with it.

2. **Teardown that runs even when the test fails.** Registration happens
   BEFORE creation and the kill happens in fixture teardown, so an
   assertion failure, an error, or a KeyboardInterrupt still cleans up.
   (A bare `subprocess.run(["tmux", "kill-session", ...])` at the end of
   a test body -- the pattern that leaked here -- runs only on success,
   which is precisely when cleanup matters least.)

3. **A janitor for runs that could not clean up at all** (SIGKILL, a
   crashed interpreter, a machine reboot mid-suite). Nothing else can
   recover those.

OWNERSHIP MARKER -- the part that makes the janitor safe to exist.
Every name this module mints looks like:

    lifecycle-own<pid>x<6 hex>-<slug>

and the janitor will kill a session ONLY when the name matches that
exact shape AND the encoded pid is no longer running. So:

  * A user's own session, a lane's session (`hp-work`, `hp1`), or any
    other suite's leftover (`test-http-secure`) can never match the
    pattern and is never touched -- not even considered.
  * A session belonging to a pytest process that is still alive -- i.e.
    another lane running this very suite concurrently -- is skipped, so
    parallel runs never kill each other's sessions.
  * Legacy fixed names (`lifecycle-shell-only` and friends) carry no
    ownership marker, so the janitor leaves them alone too. It does not
    need to remove them: no test creates those names any more, so a
    leftover one is now inert rather than fatal.

The direction of every uncertainty is deliberately "leave it alone".
A missed orphan costs one idle tmux session; a wrong kill destroys
someone's real work.
"""
from __future__ import annotations

import os
import re
import subprocess
import uuid

#: Default prefix -- the suites configure
#: `allowed_session_patterns=("lifecycle-*", ...)`, so a disposable
#: session normally has to start with it. Callers pass `prefix=` when a
#: test deliberately needs a name OUTSIDE the whitelist (e.g.
#: test_session_lifecycle's "unwhitelisted-*" cases, whose whole point is
#: that the name does not match any allowed pattern).
NAME_PREFIX = "lifecycle"

#: Identifies THIS pytest process's sessions. The pid is part of the
#: name (not just the uuid) so the janitor can ask the one question that
#: makes cleanup safe: "is the run that created this still alive?"
RUN_ID = f"{os.getpid()}x{uuid.uuid4().hex[:6]}"

#: The ownership marker is a STRUCTURAL segment, not a prefix match:
#: literal "-own", a decimal pid, "x", exactly six lowercase hex digits,
#: then "-" and a non-empty slug. Ownership is decided by that whole
#: shape, so no amount of prefix coincidence can make a real session
#: look like ours -- "lifecycle-victim" and "lifecycle-own-thing" are
#: both correctly NOT owned. The prefix is captured (non-greedy, so a
#: hyphenated one like "claude-lc" splits correctly) but is never what
#: authorises a kill.
OWNED_RE = re.compile(r"^(?P<prefix>.+?)-own(?P<pid>\d+)x(?P<tag>[0-9a-f]{6})-(?P<slug>.+)$")

#: This run's own marker segment, matched verbatim by `is_mine`.
MY_MARKER = f"-own{RUN_ID}-"


def owned_name(slug: str, *, prefix: str = NAME_PREFIX) -> str:
    """A session name unique to this run. Same `slug` in two concurrent
    runs yields two different names, so they cannot collide."""
    return f"{prefix}-own{RUN_ID}-{slug}"


def is_owned(name: str) -> bool:
    """True only for a name carrying the full ownership marker (from any
    run). Anything else -- a user session, another suite's leftover, a
    legacy fixed name, or a name that merely shares a prefix -- is not
    ours and must never be killed."""
    return bool(OWNED_RE.match(name or ""))


def owning_pid(name: str) -> int | None:
    match = OWNED_RE.match(name or "")
    return int(match.group("pid")) if match else None


def is_mine(name: str) -> bool:
    """Created by THIS pytest process. Matched on the marker segment, so
    it holds for every prefix this run mints."""
    return MY_MARKER in (name or "") and is_owned(name)


def pid_alive(pid: int) -> bool:
    """Signal 0 probes existence without touching the process. A pid we
    cannot see (ESRCH) is gone; EPERM means it exists but belongs to
    someone else, which still counts as alive -- and either way the
    conservative answer is what the caller wants, since 'alive' means
    'do not touch'."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def list_tmux_sessions() -> list[str]:
    """Every session on the host, or [] when tmux has no server running
    at all (a completely normal state, not an error)."""
    result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def kill_session(name: str) -> None:
    """Refuses outright to kill anything this module did not mint.

    A guard rather than a comment because this is the one function that
    can destroy real work: a test registering the wrong name, or a future
    edit widening a pattern, would otherwise be a silent, destructive
    bug."""
    if not is_owned(name):
        raise AssertionError(
            f"refusing to kill tmux session {name!r}: not an owned test session "
            f"(expected <prefix>-own<pid>x<6 hex>-<slug>)")
    subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


def sweep_orphans(*, sessions: list[str] | None = None,
                  pid_is_alive=pid_alive, killer=None) -> list[str]:
    """Kill owned sessions left behind by runs that are no longer
    running. Returns the names actually swept.

    `sessions`/`pid_is_alive`/`killer` are injectable so the janitor's
    own decisions are testable without creating real sessions or real
    dead processes."""
    names = list_tmux_sessions() if sessions is None else sessions
    kill = killer if killer is not None else kill_session
    swept: list[str] = []
    for name in names:
        pid = owning_pid(name)
        if pid is None:
            continue                      # not ours -- never touched
        if is_mine(name):
            continue                      # this run's own live sessions
        if pid_is_alive(pid):
            continue                      # another run is using it right now
        kill(name)
        swept.append(name)
    return swept
