"""The prompt-start watcher's install contract.

These tests exist because of a specific, live failure mode: the watcher's
systemd units sat in ``deploy/systemd/`` looking perfectly real, while on the
canonical controller ``systemctl --user is-enabled
terminal-mcp-prompt-start-watcher.timer`` answered ``not-found`` and the
recovery cycle had never run once.  The units hardcoded an absolute
``/home/dell/...`` ExecStart from a retired host, so no copy of them could
ever have started -- and nothing in the repo or the suite noticed, because
"a unit file exists" and "a unit is installable" were never the same claim and
nothing asserted the second one.

So: assert the second one.
"""
from __future__ import annotations

import os
import re
import stat
import tomllib
from pathlib import Path

import pytest

from terminal_mcp.prompt_start_watcher import (CONSOLE_SCRIPT, UNIT_NAMES, render_unit,
                                               render_units, watcher_exec_start)

REPO = Path(__file__).resolve().parent.parent
UNIT_DIR = REPO / "deploy/systemd"
INSTALLER = REPO / "deploy/install-prompt-start-watcher.sh"
SERVICE, TIMER = UNIT_NAMES


def _uncommented(text: str) -> str:
    """The file with its comment lines dropped -- these units and this
    installer are heavily commented, and a word in a comment is not a
    directive."""
    return "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))


def _directive(text: str, name: str) -> str | None:
    match = re.search(rf"(?m)^{name}=(.*)$", text)
    return match.group(1) if match else None


def _fake_venv(root: Path, *, with_console_script: bool) -> Path:
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").write_text("#!/bin/sh\n")
    if with_console_script:
        (venv / "bin" / CONSOLE_SCRIPT).write_text("#!/bin/sh\n")
    return venv


# ---------------------------------------------------------------------------
# The shipped templates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", UNIT_NAMES)
def test_shipped_unit_hardcodes_no_foreign_home(name):
    """The original bug, pinned: an absolute /home/<someone> path in a unit
    that every other host is expected to install."""
    text = (UNIT_DIR / name).read_text(encoding="utf-8")
    offenders = [line for line in _uncommented(text).splitlines()
                 if re.search(r"/home/[A-Za-z0-9._-]+", line)]
    assert offenders == [], f"{name} hardcodes a host-specific home: {offenders}"


def test_shipped_service_is_a_bounded_oneshot_ordered_after_the_live_controller():
    text = (UNIT_DIR / SERVICE).read_text(encoding="utf-8")
    assert _directive(text, "Type") == "oneshot"
    # terminal-mcp-fed-controller.service is the retired Dell unit name; the
    # live controller unit is terminal-mcp-http.service.
    assert _directive(text, "After") == "terminal-mcp-http.service"
    # A Restart= on a oneshot recovery cycle is an Enter storm against a live
    # pane. The timer owns the cadence; nothing else may.
    assert _directive(text, "Restart") is None
    # Enabling the service directly must fail loudly rather than create a
    # second, timer-less activation path.  (The file explains that in a
    # comment, hence comparing directives rather than raw text.)
    assert "[Install]" not in _uncommented(text)


def test_shipped_timer_installs_itself_and_names_its_service():
    text = (UNIT_DIR / TIMER).read_text(encoding="utf-8")
    assert _directive(text, "WantedBy") == "timers.target"
    assert _directive(text, "Unit") == SERVICE
    assert _directive(text, "OnUnitActiveSec") is not None


def test_console_script_entry_point_exists_and_resolves():
    """The unit calls a console script; `pip install -e .` must actually make
    one.  Every other entry point in this project has one -- this one did not,
    which is why the unit had to reach for `python -m` plus a PYTHONPATH."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts[CONSOLE_SCRIPT] == "terminal_mcp.prompt_start_watcher:main"
    module, _, attr = scripts[CONSOLE_SCRIPT].partition(":")
    import importlib
    assert callable(getattr(importlib.import_module(module), attr))


# ---------------------------------------------------------------------------
# Rendering for one concrete host
# ---------------------------------------------------------------------------

def test_rendered_service_points_at_a_binary_that_exists(tmp_path):
    venv = _fake_venv(tmp_path, with_console_script=True)
    rendered = render_units(repo_dir=REPO, venv_dir=venv,
                            config_path=tmp_path / "watcher.json")[SERVICE]
    exec_start = _directive(rendered, "ExecStart")
    assert exec_start is not None
    binary = Path(exec_start.split()[0])
    assert binary.is_absolute()
    assert binary.exists(), f"rendered ExecStart points at a missing file: {binary}"
    assert binary == venv / "bin" / CONSOLE_SCRIPT
    assert _directive(rendered, "WorkingDirectory") == str(REPO)


def test_rendering_falls_back_to_python_m_when_the_venv_predates_the_script(tmp_path):
    """An existing controller venv that has not been re-installed since the
    entry point was added must still get a working unit, not a dangling one."""
    venv = _fake_venv(tmp_path, with_console_script=False)
    exec_start = watcher_exec_start(venv, config_path=tmp_path / "watcher.json")
    assert exec_start.startswith(f"{venv / 'bin/python'} -m terminal_mcp.prompt_start_watcher")
    assert Path(exec_start.split()[0]).exists()


@pytest.mark.parametrize("name", UNIT_NAMES)
def test_rendered_units_carry_no_relative_or_placeholder_paths(tmp_path, name):
    venv = _fake_venv(tmp_path, with_console_script=True)
    rendered = render_units(repo_dir=REPO, venv_dir=venv,
                            config_path=tmp_path / "watcher.json")[name]
    for directive in ("WorkingDirectory", "ExecStart", "Documentation"):
        value = _directive(rendered, directive)
        if value is None:
            continue
        assert "%h" not in value, f"{name}: {directive} still has an unresolved %h"
        assert "@" not in value, f"{name}: {directive} still has a placeholder"


def test_rendering_a_relative_repo_dir_still_produces_absolute_paths(tmp_path, monkeypatch):
    """systemd rejects a relative WorkingDirectory outright, and a unit that
    refuses to load looks exactly like one that was never installed."""
    venv = _fake_venv(tmp_path, with_console_script=True)
    monkeypatch.chdir(REPO)
    rendered = render_units(repo_dir=".", venv_dir=venv,
                            config_path=tmp_path / "watcher.json")[SERVICE]
    assert Path(_directive(rendered, "WorkingDirectory")).is_absolute()


def test_timer_cadence_is_rendered_from_the_watcher_config_interval(tmp_path):
    venv = _fake_venv(tmp_path, with_console_script=True)
    rendered = render_units(repo_dir=REPO, venv_dir=venv, interval_seconds=25,
                            config_path=tmp_path / "watcher.json")[TIMER]
    assert _directive(rendered, "OnUnitActiveSec") == "25"
    assert _directive(rendered, "OnBootSec") == "25"


def test_rendering_is_idempotent(tmp_path):
    """Re-running the installer must converge, not accumulate."""
    venv = _fake_venv(tmp_path, with_console_script=True)
    out = tmp_path / "units"
    first = render_units(repo_dir=REPO, venv_dir=venv, config_path=tmp_path / "w.json",
                         output_dir=out)
    written_once = {n: (out / n).read_text(encoding="utf-8") for n in UNIT_NAMES}
    second = render_units(repo_dir=REPO, venv_dir=venv, config_path=tmp_path / "w.json",
                          output_dir=out)
    assert first == second == written_once
    assert {n: (out / n).read_text(encoding="utf-8") for n in UNIT_NAMES} == written_once
    for name in UNIT_NAMES:
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o644
    # No half-written temp files left behind by the atomic replace.
    assert sorted(p.name for p in out.iterdir()) == sorted(UNIT_NAMES)


def test_rendering_a_second_time_over_a_stale_unit_replaces_it(tmp_path):
    """Upgrading a host that already has the old, /home/dell-flavoured copy
    installed must overwrite it rather than leave it in place."""
    venv = _fake_venv(tmp_path, with_console_script=True)
    out = tmp_path / "units"
    out.mkdir()
    (out / SERVICE).write_text("[Service]\nExecStart=/home/dell/nope\n", encoding="utf-8")
    render_units(repo_dir=REPO, venv_dir=venv, config_path=tmp_path / "w.json", output_dir=out)
    assert "/home/dell/nope" not in (out / SERVICE).read_text(encoding="utf-8")
    assert _directive((out / SERVICE).read_text(), "ExecStart") == watcher_exec_start(venv, config_path=tmp_path / "w.json")


def test_render_unit_rewrites_each_directive_exactly_once(tmp_path):
    venv = _fake_venv(tmp_path, with_console_script=True)
    template = (UNIT_DIR / SERVICE).read_text(encoding="utf-8")
    rendered = render_unit(template, repo_dir=REPO, venv_dir=venv,
                           config_path=tmp_path / "w.json")
    assert len(re.findall(r"(?m)^ExecStart=", rendered)) == 1
    assert len(re.findall(r"(?m)^WorkingDirectory=", rendered)) == 1
    # The explanatory comments survive rendering -- an operator reading the
    # installed file should still learn why it looks the way it does.
    assert rendered.count("#") >= template.count("#") - 1


# ---------------------------------------------------------------------------
# The installer itself
# ---------------------------------------------------------------------------

def test_installer_exists_and_is_executable():
    assert INSTALLER.is_file()
    assert os.access(INSTALLER, os.X_OK), "installer must be committed with the +x bit"


def test_installer_does_the_four_things_that_make_a_unit_actually_run():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "--render-units" in text, "installer must render, never cp the template verbatim"
    assert "$HOME/.config/systemd/user" in text
    assert "systemctl --user daemon-reload" in text
    assert f'systemctl --user enable --now "$TIMER_UNIT"' in text
    assert "set -euo pipefail" in text


def test_installer_touches_no_other_unit():
    """Its blast radius is this timer and this service, nothing else."""
    text = INSTALLER.read_text(encoding="utf-8")
    mutating = re.findall(r"systemctl --user (?:enable|disable|start|stop|restart)[^\n]*", text)
    for line in mutating:
        assert "TIMER_UNIT" in line, f"installer mutates an unrelated unit: {line}"
    # Reading terminal-mcp-http.service to copy its config path is fine;
    # restarting it is not.
    assert "restart" not in _uncommented(text).casefold()


@pytest.mark.parametrize("name", UNIT_NAMES)
def test_every_shipped_real_unit_has_an_install_path(name):
    """The anti-recurrence guard.

    A unit committed under deploy/systemd with no script that installs it is
    exactly how this one stayed 'shipped but never running'. A real (non
    .example) unit must be named by an installer in deploy/.
    """
    installers = [p for p in (REPO / "deploy").rglob("*.sh")]
    assert any(name in p.read_text(encoding="utf-8") for p in installers), (
        f"{name} is shipped but no deploy/*.sh installs it -- add one, or the "
        f"unit will sit in the repo looking installed while `systemctl --user "
        f"is-enabled` says not-found")


# ---------------------------------------------------------------------------
# Refusing to install a unit that points at another machine
# ---------------------------------------------------------------------------

def test_a_stale_checkouts_foreign_home_is_refused_not_installed(tmp_path):
    """Rendering rewrites named directives, not arbitrary stale ones.  An old
    checkout's `Environment=...=/home/dell/...` would therefore survive -- and
    quietly reinstate the exact unit that never ran.  Refuse it."""
    from terminal_mcp.prompt_start_watcher import validate_rendered_unit
    stale = ("[Service]\n"
             "Environment=TERMINAL_MCP_CONFIG=/home/dell/.config/terminal-mcp-fed/config.yaml\n"
             "ExecStart=/home/kimex/venv/bin/python -m terminal_mcp.prompt_start_watcher\n")
    with pytest.raises(ValueError, match="refusing to install"):
        validate_rendered_unit(SERVICE, stale, home="/home/kimex")


def test_validation_accepts_this_hosts_own_home_and_ignores_comments():
    from terminal_mcp.prompt_start_watcher import validate_rendered_unit
    ok = ("# never hardcode /home/dell/anything here\n"
          "[Service]\n"
          "WorkingDirectory=/home/kimex/workspace/terminal-mcp\n"
          "EnvironmentFile=-%h/.config/terminal-mcp/prompt-start-watcher.env\n")
    validate_rendered_unit(SERVICE, ok, home="/home/kimex")


def test_the_shipped_templates_render_clean_on_a_host_they_never_heard_of(tmp_path):
    """Install the shipped templates into a checkout under a *different* home
    and they must validate against that home -- i.e. the only home the rendered
    units mention is the one being installed onto."""
    import shutil
    from terminal_mcp.prompt_start_watcher import validate_rendered_unit
    fake_home = tmp_path / "home/somebody-else"
    repo = fake_home / "workspace/terminal-mcp"
    (repo / "deploy").mkdir(parents=True)
    shutil.copytree(UNIT_DIR, repo / "deploy/systemd")
    venv = _fake_venv(repo, with_console_script=True)
    for name, text in render_units(repo_dir=repo, venv_dir=venv,
                                   config_path=repo / "w.json", home=fake_home).items():
        validate_rendered_unit(name, text, home=fake_home)
        assert "/home/kimex" not in text


def test_render_units_cli_exits_nonzero_instead_of_tracebacking(tmp_path, capsys, monkeypatch):
    """An installer must say what is wrong, not hand an operator a traceback."""
    from terminal_mcp import prompt_start_watcher as psw
    stale_repo = tmp_path / "stale"
    (stale_repo / "deploy/systemd").mkdir(parents=True)
    for name in UNIT_NAMES:
        (stale_repo / "deploy/systemd" / name).write_text(
            "[Service]\nEnvironment=X=/home/someone-else/x\n", encoding="utf-8")
    venv = _fake_venv(tmp_path, with_console_script=True)
    code = psw.main(["--render-units", str(tmp_path / "out"), "--repo-dir", str(stale_repo),
                     "--venv-dir", str(venv), "--config", str(tmp_path / "w.json")])
    assert code == 2
    assert "refusing to install" in capsys.readouterr().err
    assert not (tmp_path / "out").exists(), "a refused render must write nothing"
