"""Herdr runtime adapter (P0 pilot): read Herdr panes/agents next to tmux.

WHY THIS EXISTS
---------------
Herdr (https://github.com/herdrdev/herdr) is a terminal multiplexer that
classifies coding agents in its panes as idle/working/blocked/done/unknown.
The pilot runs ONE named Herdr session on one node and shows it beside the
tmux sessions Terminal MCP already manages. tmux stays the default runtime;
nothing here creates, sends to or adopts a tmux session.

SAFETY RULES
------------
  * Off unless HERDR_ENABLED=1 AND the local node is in
    TERMINAL_MCP_HERDR_NODES. Disabled means zero subprocesses.
  * Every call is `herdr --session <pilot> ...` as an argv list (no shell),
    with a hard timeout. A failure opens a short circuit breaker so a hung or
    missing Herdr never slows the page that polls it; callers get a
    `degraded` payload and tmux rows are untouched (fallback).
  * The `default` Herdr session and `herdr server stop` are never used.
  * Mutations (close a pane, stop the pilot session) need an owner file
    written when the pilot was provisioned, a workspace label carrying the
    owner prefix, and an explicit confirm equal to the target. They are only
    reachable from the CLI below, never from HTTP.
  * Herdr's API is a 0600 unix socket; this module never opens a listener.

Herdr's JSON has no timestamps, so first_seen_at/state_changed_at are what
this process observed (in memory, like live_sessions.py).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SOURCE = "herdr"
DEFAULT_BIN = "~/.local/opt/herdr/v0.9.5/herdr"
DEFAULT_SESSION = "terminalmcp-pilot"
DEFAULT_CONFIG_PATH = "~/.config/terminal-mcp/herdr-pilot/config.toml"
DEFAULT_STATE_DIR = "~/.local/state/terminal-mcp/herdr-pilot"
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_CACHE_SECONDS = 5.0
BREAKER_SECONDS = 30.0
OWNER = "terminal-mcp"
OWNER_LABEL_PREFIX = "tmcp-"

AGENT_STATES = ("working", "blocked", "idle", "done", "unknown")
_SESSION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_PANE_RE = re.compile(r"^w\d+:p\d+$")
# Inherited Herdr caller context must not retarget our calls at another pane
# or socket (this module may itself run inside a Herdr pane).
_STRIP_ENV = ("HERDR_SOCKET_PATH", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID",
              "HERDR_ENV")
_TRUE = {"1", "true", "yes", "on"}


class HerdrError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class HerdrConfig:
    enabled: bool = False
    nodes: tuple[str, ...] = ()
    binary: str = DEFAULT_BIN
    session: str = DEFAULT_SESSION
    config_path: str = DEFAULT_CONFIG_PATH
    state_dir: str = DEFAULT_STATE_DIR
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    cache_seconds: float = DEFAULT_CACHE_SECONDS
    task_map_path: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "HerdrConfig":
        env = os.environ if env is None else env

        def num(name: str, default: float) -> float:
            try:
                return max(0.1, float(env.get(name, default)))
            except ValueError:
                return default

        nodes = tuple(n.strip() for n in env.get("TERMINAL_MCP_HERDR_NODES", "").split(",")
                      if n.strip())
        return cls(
            enabled=env.get("HERDR_ENABLED", "").strip().lower() in _TRUE,
            nodes=nodes,
            binary=env.get("TERMINAL_MCP_HERDR_BIN", DEFAULT_BIN),
            session=env.get("TERMINAL_MCP_HERDR_SESSION", DEFAULT_SESSION),
            config_path=env.get("TERMINAL_MCP_HERDR_CONFIG_PATH", DEFAULT_CONFIG_PATH),
            state_dir=env.get("TERMINAL_MCP_HERDR_STATE_DIR", DEFAULT_STATE_DIR),
            timeout_seconds=num("TERMINAL_MCP_HERDR_TIMEOUT", DEFAULT_TIMEOUT_SECONDS),
            cache_seconds=num("TERMINAL_MCP_HERDR_CACHE_SECONDS", DEFAULT_CACHE_SECONDS),
            task_map_path=env.get("TERMINAL_MCP_HERDR_TASK_MAP") or None,
        )

    def active_for(self, node_id: str) -> bool:
        return self.enabled and node_id in self.nodes


Runner = Callable[[list[str], dict[str, str], float], subprocess.CompletedProcess]


def _subprocess_runner(argv: list[str], env: dict[str, str], timeout: float
                       ) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, check=False)


def normalize_state(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in AGENT_STATES else "unknown"


@dataclass
class _Observed:
    first_seen_at: float
    state: str
    state_changed_at: float
    seq: Any = None


@dataclass
class HerdrRuntimeAdapter:
    config: HerdrConfig
    node_id: str
    runner: Runner = _subprocess_runner
    clock: Callable[[], float] = time.time
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cache: tuple[float, dict[str, Any]] | None = field(default=None, repr=False)
    _last_good: dict[str, Any] | None = field(default=None, repr=False)
    _breaker_until: float = 0.0
    _observed: dict[str, _Observed] = field(default_factory=dict, repr=False)

    # -- plumbing -----------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.config.active_for(self.node_id)

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env["HERDR_SESSION"] = self.config.session
        env["HERDR_CONFIG_PATH"] = os.path.expanduser(self.config.config_path)
        return env

    def _call(self, *args: str) -> dict[str, Any]:
        if not _SESSION_RE.match(self.config.session) or self.config.session == "default":
            raise HerdrError("HERDR_SESSION_REFUSED", self.config.session)
        argv = [os.path.expanduser(self.config.binary), "--session", self.config.session, *args]
        try:
            proc = self.runner(argv, self._env(), self.config.timeout_seconds)
        except subprocess.TimeoutExpired:
            raise HerdrError("HERDR_TIMEOUT", f"{' '.join(args)} > {self.config.timeout_seconds}s")
        except FileNotFoundError:
            raise HerdrError("HERDR_NOT_INSTALLED", argv[0])
        except OSError as exc:
            raise HerdrError("HERDR_EXEC_FAILED", str(exc))
        if proc.returncode != 0:
            raise HerdrError("HERDR_CLI_ERROR",
                             (proc.stderr or proc.stdout or "").strip()[:300]
                             or f"exit {proc.returncode}")
        try:
            return json.loads(proc.stdout or "{}")
        except ValueError:
            raise HerdrError("HERDR_BAD_JSON", (proc.stdout or "")[:200])

    def _task_map(self) -> dict[str, Any]:
        path = self.config.task_map_path
        if not path:
            return {}
        try:
            data = json.loads(Path(os.path.expanduser(path)).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # -- reads --------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        if not self.active:
            return {"source": SOURCE, "enabled": False, "status": "disabled",
                    "node_id": self.node_id}
        try:
            status = self._call("status", "--json")
        except HerdrError as exc:
            return {"source": SOURCE, "enabled": True, "status": "degraded",
                    "node_id": self.node_id, "session": self.config.session,
                    "blocker": exc.code, "detail": exc.detail}
        server = status.get("server") or {}
        ok = bool(server.get("running")) and server.get("compatible", True) is not False
        return {"source": SOURCE, "enabled": True, "status": "ok" if ok else "degraded",
                "node_id": self.node_id, "session": self.config.session,
                "version": server.get("version"), "protocol": server.get("protocol"),
                "running": bool(server.get("running")),
                "blocker": None if ok else "HERDR_SERVER_NOT_RUNNING"}

    def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        """Pilot panes/agents as rows, cached and breaker-protected. Never raises."""
        if not self.active:
            return {"source": SOURCE, "enabled": False, "status": "disabled", "rows": []}
        with self._lock:
            now = self.clock()
            if not force and self._cache and now - self._cache[0] < self.config.cache_seconds:
                return self._cache[1]
            if now < self._breaker_until:
                payload = self._degraded(now, "HERDR_BREAKER_OPEN",
                                         f"retry after {self._breaker_until - now:.0f}s")
            else:
                try:
                    payload = self._build(now)
                    self._last_good = payload
                except HerdrError as exc:
                    self._breaker_until = now + BREAKER_SECONDS
                    payload = self._degraded(now, exc.code, exc.detail)
            self._cache = (now, payload)
            return payload

    def _degraded(self, now: float, code: str, detail: str) -> dict[str, Any]:
        stale = []
        for row in (self._last_good or {}).get("rows", []):
            stale.append({**row, "state": "unknown", "stale": True, "blocker": code})
        return {"source": SOURCE, "enabled": True, "status": "degraded", "blocker": code,
                "detail": detail, "session": self.config.session, "node_id": self.node_id,
                "observed_at": now, "rows": stale}

    def _build(self, now: float) -> dict[str, Any]:
        snap = (self._call("api", "snapshot").get("result") or {}).get("snapshot") or {}
        workspaces = {w.get("workspace_id"): w for w in snap.get("workspaces") or []}
        agents = {a.get("pane_id"): a for a in snap.get("agents") or []}
        task_map = self._task_map()
        rows: list[dict[str, Any]] = []
        for pane in snap.get("panes") or []:
            pane_id = pane.get("pane_id")
            agent = agents.get(pane_id) or {}
            ws = workspaces.get(pane.get("workspace_id")) or {}
            state = normalize_state(agent.get("agent_status") or pane.get("agent_status"))
            key = f"{SOURCE}:{self.node_id}/{self.config.session}/{pane_id}"
            seen = self._observed.get(key)
            seq = agent.get("state_change_seq")
            if seen is None:
                seen = self._observed[key] = _Observed(now, state, now, seq)
            elif seen.state != state or seen.seq != seq:
                seen.state, seen.seq, seen.state_changed_at = state, seq, now
            agent_name = agent.get("agent")
            mapping = (task_map.get(pane_id) or task_map.get(agent_name or "")
                       or task_map.get(ws.get("label") or "") or {})
            rows.append({
                "key": key, "source": SOURCE, "node_id": self.node_id,
                "session": self.config.session, "workspace": ws.get("label"),
                "pane_id": pane_id, "agent": agent_name, "is_agent": bool(agent),
                "state": state, "cwd": pane.get("foreground_cwd") or pane.get("cwd"),
                "title": pane.get("terminal_title_stripped"),
                "task_id": mapping.get("task_id"), "repo": mapping.get("repo"),
                "blocker": "AGENT_BLOCKED" if state == "blocked" else None,
                "observed_at": now, "first_seen_at": seen.first_seen_at,
                "state_changed_at": seen.state_changed_at, "stale": False,
            })
        keep = {r["key"] for r in rows}
        for gone in [k for k in self._observed if k not in keep]:
            del self._observed[gone]
        counts = {s: 0 for s in AGENT_STATES}
        for row in rows:
            counts[row["state"]] += 1
        return {"source": SOURCE, "enabled": True, "status": "ok", "blocker": None,
                "session": self.config.session, "node_id": self.node_id,
                "version": snap.get("version"), "observed_at": now, "rows": rows,
                "counts": counts}

    # -- scoped control (CLI only) ------------------------------------------

    def _owner_path(self) -> Path:
        return Path(os.path.expanduser(self.config.state_dir)) / "owner.json"

    def write_owner(self) -> dict[str, Any]:
        record = {"owner": OWNER, "session": self.config.session, "node_id": self.node_id,
                  "created_at": self.clock()}
        path = self._owner_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n")
        return record

    def _require_owner(self) -> None:
        if not self.active:
            raise HerdrError("HERDR_DISABLED", "HERDR_ENABLED=1 and node allowlist required")
        try:
            record = json.loads(self._owner_path().read_text())
        except (OSError, ValueError):
            raise HerdrError("HERDR_NOT_OWNER", f"no owner record at {self._owner_path()}")
        if record.get("owner") != OWNER or record.get("session") != self.config.session:
            raise HerdrError("HERDR_NOT_OWNER", f"owner record is for {record.get('session')!r}")

    def attach_command(self) -> list[str]:
        """argv an operator runs in their own terminal; detach is ctrl+b q or closing
        that client and never stops panes. Not executed here (needs a TTY)."""
        return [os.path.expanduser(self.config.binary), "session", "attach", self.config.session]

    def close_pane(self, pane_id: str, *, confirm: str) -> dict[str, Any]:
        self._require_owner()
        if not _PANE_RE.match(pane_id or ""):
            raise HerdrError("HERDR_BAD_PANE_ID", pane_id)
        if confirm != pane_id:
            raise HerdrError("CONFIRMATION_REQUIRED", f"pass confirm={pane_id}")
        snap = (self._call("api", "snapshot").get("result") or {}).get("snapshot") or {}
        pane = next((p for p in snap.get("panes") or [] if p.get("pane_id") == pane_id), None)
        if pane is None:
            raise HerdrError("HERDR_PANE_NOT_FOUND", pane_id)
        label = next((w.get("label") for w in snap.get("workspaces") or []
                      if w.get("workspace_id") == pane.get("workspace_id")), None) or ""
        if not label.startswith(OWNER_LABEL_PREFIX):
            raise HerdrError("HERDR_NOT_OWNER", f"workspace {label!r} lacks {OWNER_LABEL_PREFIX!r}")
        return self._call("pane", "close", pane_id)

    def stop_session(self, *, confirm: str) -> dict[str, Any]:
        self._require_owner()
        if confirm != self.config.session:
            raise HerdrError("CONFIRMATION_REQUIRED", f"pass confirm={self.config.session}")
        return self._call("session", "stop", self.config.session, "--json")


def unified_rows(tmux_rows: list[dict[str, Any]], herdr_payload: dict[str, Any] | None,
                 *, local_node_id: str) -> dict[str, Any]:
    """One list with source=tmux|herdr. Dedup by key; a task_id claimed by a tmux
    row keeps the tmux row primary and records the herdr pane under `also_in`."""
    rows: dict[str, dict[str, Any]] = {}
    by_task: dict[str, dict[str, Any]] = {}
    for row in tmux_rows:
        node = row.get("node_id") or local_node_id
        name = row.get("name") or row.get("session")
        entry = {"key": f"tmux:{node}/{name}", "source": "tmux", "node_id": node,
                 "session": name, "state": row.get("state") or row.get("status"),
                 "task_id": row.get("task_id"), "repo": row.get("repo") or row.get("cwd")}
        rows.setdefault(entry["key"], entry)
        if entry["task_id"]:
            by_task.setdefault(str(entry["task_id"]), entry)
    for row in (herdr_payload or {}).get("rows") or []:
        primary = by_task.get(str(row.get("task_id"))) if row.get("task_id") else None
        if primary is not None:
            primary.setdefault("also_in", []).append(row["key"])
            continue
        rows.setdefault(row["key"], row)
    herdr = herdr_payload or {"enabled": False, "status": "disabled"}
    return {"rows": list(rows.values()),
            "sources": {"tmux": {"status": "ok", "count": len(tmux_rows)},
                        SOURCE: {k: herdr.get(k) for k in ("enabled", "status", "blocker",
                                                           "detail", "session", "version")}}}


def adapter_from_env(node_id: str) -> HerdrRuntimeAdapter:
    return HerdrRuntimeAdapter(HerdrConfig.from_env(), node_id)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m terminal_mcp.herdr_runtime")
    parser.add_argument("--node-id", default=os.environ.get("TERMINAL_MCP_NODE_ID", "hp-linux"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    sub.add_parser("list")
    snap = sub.add_parser("write-snapshot", help="canary: write list output atomically")
    snap.add_argument("path")
    sub.add_parser("claim-owner", help="record that terminal-mcp provisioned the pilot")
    sub.add_parser("attach-command")
    close = sub.add_parser("close-pane")
    close.add_argument("pane_id")
    close.add_argument("--confirm", required=True)
    stop = sub.add_parser("stop-session")
    stop.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    adapter = adapter_from_env(args.node_id)
    try:
        if args.cmd == "health":
            out: Any = adapter.health()
        elif args.cmd == "list":
            out = adapter.snapshot(force=True)
        elif args.cmd == "write-snapshot":
            out = {"health": adapter.health(), "snapshot": adapter.snapshot(force=True)}
            tmp = Path(args.path + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(out, indent=2) + "\n")
            tmp.replace(args.path)
        elif args.cmd == "claim-owner":
            if not adapter.active:
                raise HerdrError("HERDR_DISABLED", "HERDR_ENABLED=1 and node allowlist required")
            out = adapter.write_owner()
        elif args.cmd == "attach-command":
            out = {"argv": adapter.attach_command()}
        elif args.cmd == "close-pane":
            out = adapter.close_pane(args.pane_id, confirm=args.confirm)
        else:
            out = adapter.stop_session(confirm=args.confirm)
    except HerdrError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail}))
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
