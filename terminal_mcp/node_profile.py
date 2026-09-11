"""Environment convergence: what a node needs to be able to take over.

Converging source code is not enough. A node can run the exact commit the
controller runs and still be useless for failover because it has no Claude
login, no tmux, or no Tailscale -- and that is invisible until the moment you
actually need it. This module reads deploy/node-profile.yaml, inventories the
local machine against it, and reports a status per requirement.

SECRETS NEVER MOVE. Every auth probe here is existence- or status-only: it
checks that a credential is present and that a backend reports itself ready.
It does not open credential files, does not read their contents, does not
compare them, and nothing it returns can be replayed as a credential. A node
missing an auth reports NEEDS_AUTH plus the one-time command to fix it, run by
a human on that machine -- which is usually a device/browser flow that could
not be copied anyway.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROFILE_PATH = Path(__file__).resolve().parent.parent / "deploy" / "node-profile.yaml"

SUPPORTED_SCHEMA_VERSION = 1

# Statuses, deliberately distinct: a caller acts differently on each.
PASS = "PASS"
MISSING = "MISSING"          # required thing is not installed at all
DRIFT = "DRIFT"              # present, but the wrong version
NEEDS_AUTH = "NEEDS_AUTH"    # installed, but not logged in / not joined
SKIPPED = "SKIPPED"          # not applicable to this OS or role
UNKNOWN = "UNKNOWN"          # probe itself could not run; never reported PASS

_PROBE_TIMEOUT_SECONDS = 8.0


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class Check:
    """One requirement's result. `detail` is always safe to log and ship."""
    id: str
    kind: str                 # tool | service | port | auth
    status: str
    required: bool
    detail: str = ""
    found_version: str | None = None
    wanted_version: str | None = None
    remediation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "status": self.status,
                "required": self.required, "detail": self.detail,
                "found_version": self.found_version, "wanted_version": self.wanted_version,
                "remediation": self.remediation}


def current_os() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "darwin"
    return "linux"


def load_profile(path: Path | str | None = None) -> dict[str, Any]:
    """Parse and version-check the profile.

    A schema version this code does not implement is an ERROR, never a
    best-effort read: silently ignoring a requirement shape it does not
    understand is how a node passes an audit it should have failed.
    """
    target = Path(path) if path else PROFILE_PATH
    if not target.exists():
        raise ProfileError(f"node profile not found: {target}")
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    schema = data.get("schema_version")
    if schema != SUPPORTED_SCHEMA_VERSION:
        raise ProfileError(
            f"node profile schema_version {schema!r} is not supported by this build "
            f"(expects {SUPPORTED_SCHEMA_VERSION}) -- refusing to guess at its shape")
    for key in ("profile_version", "roles", "auth", "minimum_failover_auth_set"):
        if key not in data:
            raise ProfileError(f"node profile is missing required key {key!r}")
    return data


def profile_fingerprint(profile: dict[str, Any]) -> str:
    """Stable hash of the profile's CONTENT, so two nodes can compare what
    they were audited against without shipping the file around."""
    import hashlib
    canonical = json.dumps(profile, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# -- version comparison -----------------------------------------------------

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def parse_version(text: str) -> tuple[int, ...]:
    """First dotted-numeric run in the output, e.g. 'tmux 3.4a' -> (3, 4).

    Tools print versions in wildly different shapes; this deliberately reads
    only the numeric prefix and ignores suffixes like 'a' or '-rc1' rather
    than trying to implement full semver for every CLI on the box.
    """
    match = _VERSION_RE.search(text or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def version_satisfies(found: str, minimum: str) -> bool:
    got, want = parse_version(found), parse_version(minimum)
    if not got or not want:
        return False
    return got >= want


def _run(command: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=_PROBE_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# -- tool / service / port checks -------------------------------------------

def _applies(entry: dict[str, Any], os_name: str, roles: tuple[str, ...] = ()) -> bool:
    allowed = entry.get("os")
    if allowed and os_name not in allowed:
        return False
    # `unless_roles`: a requirement that stops applying once the node also
    # holds another role -- a controller serves its own machine in-process and
    # must NOT also run a node agent against itself.
    unless = set(entry.get("unless_roles") or ())
    return not (unless & set(roles))


def check_tool(entry: dict[str, Any], os_name: str, roles: tuple[str, ...] = ()) -> Check:
    name = entry["name"]
    required = bool(entry.get("required", False))
    if not _applies(entry, os_name, roles):
        return Check(name, "tool", SKIPPED, required, detail=f"not applicable on {os_name}")
    binary = entry.get("binary", name)
    if shutil.which(binary) is None:
        return Check(name, "tool", MISSING, required,
                     detail=f"{binary!r} not on PATH",
                     remediation=entry.get("install_hint") or f"install {binary}")
    found = None
    if entry.get("version_command"):
        code, output = _run(list(entry["version_command"]))
        if code == 0:
            found = output.strip().splitlines()[0] if output.strip() else None
    minimum = entry.get("min_version")
    if minimum and found is not None and not version_satisfies(found, minimum):
        return Check(name, "tool", DRIFT, required,
                     detail=f"installed {found!r} is older than required",
                     found_version=found, wanted_version=minimum,
                     remediation=f"upgrade {binary} to >= {minimum}")
    return Check(name, "tool", PASS, required, detail=f"{binary} present",
                 found_version=found, wanted_version=minimum)


def check_service(entry: dict[str, Any], os_name: str, roles: tuple[str, ...] = ()) -> Check:
    name = entry["name"]
    if not _applies(entry, os_name, roles):
        return Check(name, "service", SKIPPED, True,
                     detail=f"not applicable on {os_name} for roles {list(roles) or ['node']}")
    kind = entry.get("kind")
    wanted = entry.get("must_be", "enabled")
    if kind == "systemd-user":
        env = dict(os.environ)
        # systemctl --user needs the user bus; a session spawned by the node
        # agent has neither, and its error text reads like empty output if you
        # do not set this. That mis-read cost a wrong audit finding once.
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        try:
            proc = subprocess.run(["systemctl", "--user", "is-enabled", name],
                                  capture_output=True, text=True, env=env,
                                  timeout=_PROBE_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return Check(name, "service", UNKNOWN, True, detail=f"probe failed: {exc}")
        state = (proc.stdout or proc.stderr or "").strip().splitlines()[:1]
        state = state[0] if state else ""
        if state == "enabled":
            return Check(name, "service", PASS, True, detail="enabled (returns after reboot)")
        return Check(name, "service", MISSING, True,
                     detail=f"is-enabled reported {state or 'nothing'}; wanted {wanted}",
                     remediation=f"systemctl --user enable {name}")
    # launchd / windows task probing needs the host itself; report UNKNOWN
    # rather than inventing a PASS.
    return Check(name, "service", UNKNOWN, True,
                 detail=f"no probe implemented for kind={kind!r} on {os_name}")


# -- auth probes: existence/status ONLY -------------------------------------

def _expand(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path)))


def _probe_auth(probe: dict[str, Any]) -> tuple[bool, str]:
    """Returns (ready, non-sensitive detail).

    Every branch is existence- or status-only. Nothing here opens a credential
    file or echoes its contents; the most a caller ever learns is that a path
    exists or that a backend reports itself running.
    """
    kind = probe.get("kind")
    if kind == "file_exists":
        for raw in probe.get("paths", []):
            if _expand(raw).exists():
                # The PATH is reported, never the contents.
                return True, f"credential present at {raw}"
        return False, "no credential file found at any known path"
    if kind == "glob_exists":
        import glob as _glob
        for pattern in probe.get("patterns", []):
            if _glob.glob(os.path.expanduser(os.path.expandvars(pattern))):
                return True, f"credential present matching {pattern}"
        return False, "no credential file matched any known pattern"
    if kind == "command_ok":
        code, _ = _run(list(probe.get("command", [])))
        return (code == 0), ("command reported success" if code == 0
                             else f"command exited {code}")
    if kind == "command_json":
        code, output = _run(list(probe.get("command", [])))
        if code != 0:
            return False, f"command exited {code}"
        try:
            data = json.loads(output)
        except ValueError:
            return False, "command output was not JSON"
        rule = probe.get("ready_when") or {}
        field_name, expected = rule.get("field"), rule.get("equals")
        actual = data.get(field_name)
        return (actual == expected), f"{field_name}={actual!r}"
    if kind == "any_of":
        details = []
        for sub in probe.get("probes", []):
            ok, detail = _probe_auth(sub)
            if ok:
                return True, detail
            details.append(detail)
        return False, "; ".join(details) or "no probe matched"
    return False, f"unknown probe kind {kind!r}"


def check_auth(entry: dict[str, Any]) -> Check:
    auth_id = entry["id"]
    ready, detail = _probe_auth(entry.get("probe") or {})
    if ready:
        return Check(auth_id, "auth", PASS, False,
                     detail=f"{entry.get('provider', '?')}: {detail}")
    return Check(auth_id, "auth", NEEDS_AUTH, False,
                 detail=f"{entry.get('provider', '?')}: {detail}",
                 remediation=entry.get("setup_command"))


# -- the whole inventory ----------------------------------------------------

def inventory(roles: tuple[str, ...] = ("node",), *, profile: dict[str, Any] | None = None,
              os_name: str | None = None) -> dict[str, Any]:
    """Audit this machine against the profile for the roles it holds."""
    data = profile or load_profile()
    os_name = os_name or current_os()
    checks: list[Check] = []
    seen: set[tuple[str, str]] = set()

    for role in roles:
        spec = data.get("roles", {}).get(role)
        if spec is None:
            checks.append(Check(role, "role", UNKNOWN, True, detail="role not defined in profile"))
            continue
        for entry in list(spec.get("tools", [])) + list(spec.get("optional_tools", [])):
            key = ("tool", entry["name"])
            if key in seen:
                continue
            seen.add(key)
            checks.append(check_tool(entry, os_name, roles))
        for entry in spec.get("services", []):
            key = ("service", entry["name"])
            if key in seen:
                continue
            seen.add(key)
            checks.append(check_service(entry, os_name, roles))

    for entry in data.get("skill_packs", []):
        checks.append(check_skill_pack(entry))

    for entry in data.get("auth", []):
        checks.append(check_auth(entry))

    required_failover = set()
    for role in roles:
        required_failover.update(data.get("minimum_failover_auth_set", {}).get(role, []))
    by_id = {c.id: c for c in checks}
    missing_failover = sorted(
        auth_id for auth_id in required_failover
        if by_id.get(auth_id) is None or by_id[auth_id].status != PASS)

    blocking = [c for c in checks
                if c.required and c.status in (MISSING, DRIFT, UNKNOWN)]
    failover_ready = not blocking and not missing_failover

    return {
        "os": os_name,
        "roles": list(roles),
        "profile_version": data.get("profile_version"),
        "schema_version": data.get("schema_version"),
        "profile_fingerprint": profile_fingerprint(data),
        "checks": [c.as_dict() for c in checks],
        "summary": {status: sum(1 for c in checks if c.status == status)
                    for status in (PASS, MISSING, DRIFT, NEEDS_AUTH, SKIPPED, UNKNOWN)},
        "blocking": [c.id for c in blocking],
        "minimum_failover_auth_set": sorted(required_failover),
        "missing_failover_auth": missing_failover,
        # The one field a fleet view should key on. It is deliberately false
        # when anything required is UNKNOWN: an unprobeable requirement is not
        # a passing one.
        "failover_ready": failover_ready,
    }


# -- CLI --------------------------------------------------------------------

def _format_human(result: dict[str, Any]) -> str:
    lines = [
        f"Node environment audit  ({result['os']}, roles={', '.join(result['roles'])})",
        f"  profile v{result['profile_version']} schema v{result['schema_version']} "
        f"fingerprint {result['profile_fingerprint']}",
        "",
    ]
    order = {MISSING: 0, DRIFT: 1, NEEDS_AUTH: 2, UNKNOWN: 3, PASS: 4, SKIPPED: 5}
    for check in sorted(result["checks"], key=lambda c: (order.get(c["status"], 9), c["id"])):
        if check["status"] == SKIPPED:
            continue
        mark = {PASS: "ok  ", MISSING: "MISS", DRIFT: "DRIF", NEEDS_AUTH: "AUTH",
                UNKNOWN: "????"}.get(check["status"], "    ")
        lines.append(f"  [{mark}] {check['id']:<24} {check['detail']}")
        if check["remediation"] and check["status"] != PASS:
            lines.append(f"         -> {check['remediation']}")
    lines.append("")
    if result["missing_failover_auth"]:
        lines.append("  FAILOVER BLOCKED -- missing required auth: "
                     + ", ".join(result["missing_failover_auth"]))
    if result["blocking"]:
        lines.append("  FAILOVER BLOCKED -- unmet requirements: " + ", ".join(result["blocking"]))
    lines.append(f"  FAILOVER_READY: {result['failover_ready']}")
    return "\n".join(lines)


def check_skill_pack(entry: dict[str, Any]) -> Check:
    """Is a prompt-level skill pack installed on this machine, for which host?

    Reuses skill_provider's reader rather than restating where gstack puts
    its files -- two answers to that question would drift, and the doctor's
    job is to report the same truth the resolver acts on.

    Never `required`: a node without a skill pack runs sessions perfectly
    well. This is reported so a project that has enabled a stage can see
    whether the node it routes to can serve it.
    """
    from .skill_provider import STATUS_DRIFT, STATUS_READY, GstackProvider

    pack_id = entry["id"]
    if entry.get("provider") != "gstack":
        return Check(pack_id, "skill_pack", UNKNOWN, False,
                     detail=f"no reader for provider {entry.get('provider')!r}")
    provider = GstackProvider(minimum_version=entry.get("min_version"))
    hosts = tuple(entry.get("hosts") or ("claude",))
    reports = {host: provider.status(host) for host in hosts}
    ready = sorted(host for host, status in reports.items() if status.status == STATUS_READY)
    if ready:
        version = next(reports[host].version for host in ready)
        return Check(pack_id, "skill_pack", PASS, False,
                     detail=f"available for {', '.join(ready)}",
                     found_version=version, wanted_version=entry.get("min_version"))
    drifted = [host for host, status in reports.items() if status.status == STATUS_DRIFT]
    if drifted:
        status = reports[drifted[0]]
        return Check(pack_id, "skill_pack", DRIFT, False, detail=status.detail,
                     found_version=status.version, wanted_version=entry.get("min_version"),
                     remediation=entry.get("install_hint"))
    detail = "; ".join(f"{host}: {status.status.lower()}" for host, status in sorted(reports.items()))
    return Check(pack_id, "skill_pack", MISSING, False, detail=detail,
                 wanted_version=entry.get("min_version"),
                 remediation=entry.get("install_hint"))


def detect_roles() -> tuple[str, ...]:
    """The roles this machine actually holds, rather than an assumption.

    Defaulting to ("node",) makes the audit WRONG on a controller: the
    node-agent requirement is `unless_roles: [controller]`, so a
    controller (which serves its own machine in-process and must not run
    an agent against itself) was told its correctly-disabled agent was a
    failover blocker. An audit that reports a deliberate configuration as
    a fault is worse than no audit -- the same class of mis-read as
    running `systemctl --user` with no user bus and believing the empty
    answer.

    Detection is by what is INSTALLED as a unit, not by what is running:
    a controller stopped for maintenance still holds the role.
    """
    roles = ["node"]
    if _has_systemd_user_unit("terminal-mcp-http"):
        roles.append("controller")
    return tuple(roles)


def _has_systemd_user_unit(unit: str) -> bool:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        proc = subprocess.run(["systemctl", "--user", "is-enabled", unit],
                              capture_output=True, text=True, env=env,
                              timeout=_PROBE_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError, AttributeError):
        return False
    # systemd answers a missing unit with exit code 4 and the literal
    # "not-found"; every other state (disabled/static/masked/enabled) means
    # the unit file IS installed here, which is what holding a role means.
    if proc.returncode == 4:
        return False
    state = ((proc.stdout or "") + (proc.stderr or "")).strip().lower()
    return bool(state) and "not-found" not in state


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="terminal-mcp-node-doctor",
        description="Audit this machine against deploy/node-profile.yaml. "
                    "Reports PASS/MISSING/DRIFT/NEEDS_AUTH per requirement. "
                    "Never reads, prints or transmits credential contents.")
    parser.add_argument("--role", action="append", dest="roles",
                        help="role this node holds (repeatable); default: detected "
                             "from the units installed on this machine")
    parser.add_argument("--profile", default=None, help="path to a profile file")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    roles = tuple(args.roles) if args.roles else detect_roles()
    try:
        result = inventory(roles, profile=load_profile(args.profile))
    except ProfileError as exc:
        print(f"error: {exc}")
        return 2
    print(json.dumps(result, indent=2) if args.json else _format_human(result))
    # Exit code is about FAILOVER readiness specifically, so this is usable as
    # a gate in a script: advisory auth gaps do not fail it.
    return 0 if result["failover_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
