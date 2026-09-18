"""Durable, fail-closed prompt-start watcher entry point.

The HTTP controller already owns the verified-submit implementation.  This
small timer-facing process reuses that exact recovery path and durable
submission database, so a controller reconnect cannot disable recovery.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

from .config import load_config
from .core import TerminalService
from .submit_watchdog import SubmissionStore

CONFIG_PATH = Path.home() / ".config/terminal-mcp/prompt-start-watcher.json"
STATE_PATH = Path.home() / ".local/state/terminal-mcp/prompt-start-watcher.json"
LOCK_PATH = Path.home() / ".local/state/terminal-mcp/prompt-start-watcher.lock"
DEFAULTS = {"enabled": True, "max_enters": 6, "interval_seconds": 10,
            "include_sessions": [], "exclude_sessions": []}


def load_watcher_config(path: str | Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    result = dict(DEFAULTS)
    result.update({key: raw[key] for key in DEFAULTS if key in raw})
    result["enabled"] = bool(result["enabled"])
    result["max_enters"] = max(1, min(6, int(result["max_enters"])))
    result["interval_seconds"] = max(3, int(result["interval_seconds"]))
    result["include_sessions"] = [str(x) for x in result["include_sessions"] if str(x)]
    result["exclude_sessions"] = [str(x) for x in result["exclude_sessions"] if str(x)]
    return result


class PromptStartWatcher:
    def __init__(self, *, config_path: str | Path = CONFIG_PATH,
                 state_path: str | Path = STATE_PATH) -> None:
        self.config_path, self.state_path = Path(config_path), Path(state_path)

    def _eligible(self, session: str, cfg: dict[str, Any]) -> bool:
        include = cfg["include_sessions"]
        return (not include or session in include) and session not in set(cfg["exclude_sessions"])

    def run_once(self) -> dict[str, Any]:
        cfg = load_watcher_config(self.config_path)
        result: dict[str, Any] = {"enabled": cfg["enabled"], "tracked_count": 0,
                                  "started_count": 0, "waiting_approval_count": 0,
                                  "stuck_count": 0, "recoveries": [], "last_run_at": time.time()}
        if not cfg["enabled"]:
            self._write_state(result)
            return result
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        LOCK_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with LOCK_PATH.open("w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result["skipped"] = "cycle_already_running"
                self._write_state(result)
                return result
            app = TerminalService(load_config())
            try:
                records = [r for r in app.submissions.active() if self._eligible(r.session, cfg)]
                result["tracked_count"] = len(records)
                for record in records:
                    before = record.enter_count
                    app.recover_submission(record)
                    after = app.submissions.get(record.submission_id)
                    if after is None:
                        continue
                    if after.execution_started:
                        result["started_count"] += 1
                    if after.ack_state == "BLOCKED_APPROVAL":
                        result["waiting_approval_count"] += 1
                    if after.ack_state == "STUCK":
                        result["stuck_count"] += 1
                    if after.enter_count > before:
                        result["recoveries"].append({"submission_id": record.submission_id,
                                                     "session": record.session,
                                                     "enter_count": after.enter_count,
                                                     "evidence": list(after.evidence[-2:])})
            finally:
                app.stop_submission_sweeper()
        self._write_state(result)
        return result

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Terminal MCP verified prompt-start watcher")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    PromptStartWatcher(config_path=args.config).run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
