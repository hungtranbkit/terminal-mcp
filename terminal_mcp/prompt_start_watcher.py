"""Durable, fail-closed prompt-start watcher entry point.

The HTTP controller already owns the verified-submit implementation.  This
small timer-facing process reuses that exact recovery path and durable
submission database, so a controller reconnect cannot disable recovery.

This module is also where the watcher's own systemd units are *rendered*.
The shipped ``deploy/systemd/terminal-mcp-prompt-start-watcher.{service,timer}``
files are host-agnostic (``%h``-relative), and rendering rewrites the two
host-dependent lines -- ``WorkingDirectory=`` and ``ExecStart=`` -- with the
real checkout/venv of the host being installed onto.  Keeping that here rather
than in shell is deliberate: it is the only way a test can prove the installed
unit points at a binary that exists, which is exactly the failure this unit
shipped with for its whole life (an absolute ``/home/dell/...`` ExecStart on a
host with no ``/home/dell``, so the timer was never installable and never ran).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .config import load_config
from .core import TerminalService
from .orchestration_policy import PROMPT_RETRY_CAP
from .submit_watchdog import SubmissionStore

CONFIG_PATH = Path.home() / ".config/terminal-mcp/prompt-start-watcher.json"
STATE_PATH = Path.home() / ".local/state/terminal-mcp/prompt-start-watcher.json"
#: The default single-cycle mutex.  Kept as a module constant for callers that
#: want to name it, but PromptStartWatcher derives its own from its state path
#: (they agree for the default state path) -- see PromptStartWatcher.__init__.
LOCK_PATH = Path.home() / ".local/state/terminal-mcp/prompt-start-watcher.lock"

#: The watcher's own Enter budget per submission.  It is the SAME six the
#: server-level orchestration policy advertises to clients
#: (``orchestration_policy.PROMPT_RETRY_CAP``) and the same six
#: ``submit_watchdog.WatchdogConfig`` defaults to, imported rather than
#: restated so the three cannot drift into three different numbers.
MAX_ENTERS_CAP = PROMPT_RETRY_CAP

DEFAULTS = {"enabled": True, "max_enters": MAX_ENTERS_CAP, "interval_seconds": 10,
            "include_sessions": [], "exclude_sessions": []}

#: Unit file names, in install order (the service must exist before the timer
#: that activates it is enabled).
UNIT_NAMES = ("terminal-mcp-prompt-start-watcher.service",
              "terminal-mcp-prompt-start-watcher.timer")

#: Where the shipped templates live inside a source checkout.
UNIT_TEMPLATE_DIR = Path("deploy/systemd")

#: The console script an installed (``pip install -e .``) checkout exposes.
CONSOLE_SCRIPT = "terminal-mcp-prompt-watcher"


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
    result["max_enters"] = max(1, min(MAX_ENTERS_CAP, int(result["max_enters"])))
    result["interval_seconds"] = max(3, int(result["interval_seconds"]))
    result["include_sessions"] = [str(x) for x in result["include_sessions"] if str(x)]
    result["exclude_sessions"] = [str(x) for x in result["exclude_sessions"] if str(x)]
    return result


# ---------------------------------------------------------------------------
# systemd unit rendering
# ---------------------------------------------------------------------------

def default_repo_dir() -> Path:
    """The checkout this module was imported from."""
    return Path(__file__).resolve().parent.parent


def default_venv_dir() -> Path:
    """The virtualenv this interpreter is running in (``sys.prefix``)."""
    return Path(sys.prefix).resolve()


def watcher_exec_start(venv_dir: str | Path, *, config_path: str | Path = "%h/.config/terminal-mcp/prompt-start-watcher.json") -> str:
    """The ExecStart= command line for a given venv.

    Prefers the console script, but falls back to ``python -m`` when the venv
    predates the ``[project.scripts]`` entry (an existing controller venv that
    has not been re-installed yet).  Rendering an ExecStart that points at a
    file which does not exist is the whole bug class this function avoids, so
    it checks rather than assumes.
    """
    venv = Path(venv_dir).resolve()
    script = venv / "bin" / CONSOLE_SCRIPT
    if script.exists():
        return f"{script} --config {config_path}"
    return f"{venv / 'bin' / 'python'} -m terminal_mcp.prompt_start_watcher --config {config_path}"


def render_unit(template: str, *, repo_dir: str | Path, venv_dir: str | Path,
                interval_seconds: int = 10,
                config_path: str | Path = "%h/.config/terminal-mcp/prompt-start-watcher.json") -> str:
    """Rewrite a shipped unit template for one concrete host.

    Only whole, self-contained directive lines are replaced, each of which
    appears exactly once in its template; comments and everything else are
    carried through untouched so the rendered file still explains itself.
    """
    # systemd rejects a relative WorkingDirectory=/ExecStart= outright, and a
    # unit that fails to load is indistinguishable at a glance from one that is
    # simply not installed -- the exact confusion this whole change exists to
    # end.  Resolve once, here, rather than trusting every caller.
    repo = Path(repo_dir).resolve()
    interval = max(3, int(interval_seconds))
    substitutions = {
        "Documentation": f"file:{repo / 'docs/prompt-submission.md'}",
        "WorkingDirectory": str(repo),
        "ExecStart": watcher_exec_start(venv_dir, config_path=config_path),
        "OnBootSec": str(interval),
        "OnUnitActiveSec": str(interval),
    }
    for directive, value in substitutions.items():
        template = re.sub(rf"(?m)^{directive}=.*$", lambda _m, v=value, d=directive: f"{d}={v}",
                          template)
    return template


def validate_rendered_unit(name: str, text: str, *, home: str | Path | None = None) -> None:
    """Refuse a rendered unit that still points at somebody else's machine.

    This is the last line of defence for the original failure: the templates in
    an OLD checkout still carry `/home/dell/...` directives that rendering does
    not touch (it rewrites named directives, not arbitrary stale ones), so
    installing from a not-yet-updated repo would quietly reproduce the exact
    unit that never ran.  Fail the install instead -- a loud refusal is the one
    outcome that cannot be mistaken for a working timer.
    """
    own_home = str(Path(home) if home is not None else Path.home())
    offenders = []
    for line in text.splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        # Only a path that STARTS at /home/<user> counts.  Anchoring on a
        # directive/URI boundary keeps an unrelated path that merely contains
        # the segment (".../pytest-tmp/home/x/...") from reading as a foreign
        # home directory.
        for match in re.finditer(r"(?:(?<=^)|(?<=[\s=:]))/home/[A-Za-z0-9._-]+", line):
            if not match.group(0).startswith(own_home):
                offenders.append(line.strip())
                break
    if offenders:
        raise ValueError(
            f"{name}: rendered unit still references a home directory that is not "
            f"{own_home} -- refusing to install it. This usually means the checkout "
            f"being installed from is older than the host-agnostic units "
            f"(git pull, then re-run). Offending lines: {offenders}")


def render_units(*, repo_dir: str | Path | None = None, venv_dir: str | Path | None = None,
                 interval_seconds: int = 10,
                 config_path: str | Path = "%h/.config/terminal-mcp/prompt-start-watcher.json",
                 output_dir: str | Path | None = None,
                 home: str | Path | None = None) -> dict[str, str]:
    """Render both units.  Returns ``{unit_name: rendered_text}``.

    With ``output_dir`` the files are also written there (0644, atomically),
    which is what the installer uses; without it this is a pure function, which
    is what the tests use.  ``home`` overrides the home directory the
    foreign-path validation is measured against (default: this user's).
    """
    repo = Path(repo_dir) if repo_dir is not None else default_repo_dir()
    venv = Path(venv_dir) if venv_dir is not None else default_venv_dir()
    rendered: dict[str, str] = {}
    for name in UNIT_NAMES:
        source = repo / UNIT_TEMPLATE_DIR / name
        rendered[name] = render_unit(source.read_text(encoding="utf-8"), repo_dir=repo,
                                     venv_dir=venv, interval_seconds=interval_seconds,
                                     config_path=config_path)
        validate_rendered_unit(name, rendered[name], home=home)
    if output_dir is not None:
        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, text in rendered.items():
            tmp = target_dir / f".{name}.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, target_dir / name)
    return rendered


class PromptStartWatcher:
    def __init__(self, *, config_path: str | Path = CONFIG_PATH,
                 state_path: str | Path = STATE_PATH,
                 lock_path: str | Path | None = None) -> None:
        self.config_path, self.state_path = Path(config_path), Path(state_path)
        # Derived from the state path rather than a module constant so that a
        # redirected state directory (a test, a second controller under
        # another HOME) gets its own mutex instead of silently contending on
        # the real one -- and so the two can never disagree about which
        # cycle's state a held lock is protecting.
        self.lock_path = (Path(lock_path) if lock_path is not None
                          else self.state_path.with_suffix(".lock"))

    def _eligible(self, session: str, cfg: dict[str, Any]) -> bool:
        include = cfg["include_sessions"]
        return (not include or session in include) and session not in set(cfg["exclude_sessions"])

    def run_once(self) -> dict[str, Any]:
        cfg = load_watcher_config(self.config_path)
        result: dict[str, Any] = {"enabled": cfg["enabled"], "tracked_count": 0,
                                  "started_count": 0, "waiting_approval_count": 0,
                                  "stuck_count": 0, "capped_count": 0,
                                  "max_enters": cfg["max_enters"],
                                  "recoveries": [], "last_run_at": time.time()}
        if not cfg["enabled"]:
            self._write_state(result)
            return result
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock_path.open("w", encoding="utf-8") as lock:
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
                    # The watcher's own Enter budget, enforced BEFORE calling
                    # recovery rather than inside it.  The durable store's
                    # compare-and-increment (SubmissionStore.reserve_enter) is
                    # still the hard, cross-process cap and remains the thing
                    # that makes a duplicate Enter impossible; this check is
                    # what makes the watcher's max_enters setting mean
                    # something instead of being loaded, clamped and ignored.
                    # A capped record is left in its current state on purpose:
                    # deciding it is terminal belongs to the controller's own
                    # cap/TTL logic, which owns that transition already.
                    if record.enter_count >= cfg["max_enters"]:
                        result["capped_count"] += 1
                        continue
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
    parser.add_argument("--render-units", metavar="OUTPUT_DIR",
                        help="Render this host's systemd units into OUTPUT_DIR and exit "
                             "without running a recovery cycle (used by "
                             "deploy/install-prompt-start-watcher.sh).")
    parser.add_argument("--repo-dir", default=None,
                        help="Checkout to render units against (default: this module's own).")
    parser.add_argument("--venv-dir", default=None,
                        help="Virtualenv to render ExecStart against (default: sys.prefix).")
    args = parser.parse_args(argv)
    if args.render_units:
        cfg = load_watcher_config(args.config)
        try:
            rendered = render_units(repo_dir=args.repo_dir, venv_dir=args.venv_dir,
                                    interval_seconds=cfg["interval_seconds"],
                                    config_path=args.config, output_dir=args.render_units)
        except (OSError, ValueError) as exc:
            # An installer should say what is wrong, not hand an operator a
            # traceback and let them guess which unit refused.
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for name in rendered:
            print(str(Path(args.render_units) / name))
        return 0
    PromptStartWatcher(config_path=args.config).run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
