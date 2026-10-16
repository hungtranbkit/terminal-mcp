from dataclasses import replace
from types import SimpleNamespace
import pytest
from terminal_mcp.config import AppConfig, PermissionsConfig, InputPolicyConfig, SessionLifecycleConfig
from terminal_mcp.lifecycle import SessionLifecycleService

class MenuBackend:
    def __init__(self, kind, selected, replace_pane=False, drift=False):
        self.kind, self.selected = kind, selected
        self.exists = self.ready = False
        self.keys = []
        self.captures = 0
        self.replace_pane, self.drift = replace_pane, drift
    def get_session(self, name):
        if not self.exists: return None
        pane = "%2" if self.replace_pane and self.captures else "%1"
        return SimpleNamespace(session_id="$1", pane_id=pane, created_epoch=1, pane_dead=False, pane_current_command="codex")
    def new_session(self, *args, **kwargs):
        self.exists = True
        return False, None
    def capture_lines(self, *args, **kwargs):
        self.captures += 1
        if self.drift and self.captures >= 2:
            return ["Unexpected security confirmation", "1. Proceed", "› 2. Cancel"]
        if self.ready: return ["› Ask Codex to do anything", "codex default"]
        labels = (["Run without daemon this time", "Restart with these settings", "Cancel"] if self.kind == "daemon" else ["Review hooks", "Trust all and continue", "Continue without trusting (hooks won’t run)"])
        return (["Background server has incompatible feature settings"] if self.kind == "daemon" else ["Hooks need review"]) + [("› " if n == self.selected else "  ") + str(n+1) + ". " + label for n, label in enumerate(labels)]
    def send_keys(self, name, keys):
        self.keys.extend(keys)
        for key in keys:
            if key == "Up": self.selected -= 1
            elif key == "Down": self.selected += 1
            elif key == "Enter":
                assert self.selected == (0 if self.kind == "daemon" else 2), "unsafe menu selection"
                self.ready = True

def configured(tmp_path):
    return AppConfig(permissions=PermissionsConfig(True, True), allowed_session_patterns=("codex-nav-*",), input_policy=InputPolicyConfig(allowed_session_patterns=("codex-nav-*",)), session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),), launch_commands=(("codex", "codex"),), create_ready_timeout_seconds=2.0))

def launch(tmp_path, monkeypatch, backend, config=None):
    svc = SessionLifecycleService(config or configured(tmp_path), backend)
    monkeypatch.setattr(svc, "launch_command_for", lambda agent: "codex")
    return svc.create("codex-nav-check", "codex", str(tmp_path))

@pytest.mark.parametrize("kind,selected,expected", [("daemon",2,["Up","Up","Enter"]),("daemon",1,["Up","Enter"]),("hooks",0,["Down","Down","Enter"]),("hooks",1,["Down","Enter"])])
def test_navigate_only_known_non_granting_choices(tmp_path, monkeypatch, kind, selected, expected):
    backend = MenuBackend(kind, selected)
    result = launch(tmp_path, monkeypatch, backend)
    assert result["state"] == "CREATED"
    assert backend.keys == expected

@pytest.mark.parametrize("denial",["terminal_input","allow_send_keys","pattern","key"])
def test_no_startup_input_without_existing_permission(tmp_path, monkeypatch, denial):
    config = configured(tmp_path)
    if denial in ("terminal_input","allow_send_keys"):
        config = replace(config, permissions=replace(config.permissions, **{denial:False}))
    elif denial == "pattern":
        config = replace(config, input_policy=replace(config.input_policy, denied_session_patterns=("codex-nav-*",)))
    else:
        config = replace(config, input_policy=replace(config.input_policy, allow_keys=("Up","Down")))
    backend = MenuBackend("daemon",0)
    result = launch(tmp_path,monkeypatch,backend,config)
    assert result["state"] == "BLOCKED"
    assert backend.keys == []

@pytest.mark.parametrize("mode",["replace_pane","drift"])
def test_recheck_identity_and_menu_before_startup_key(tmp_path,monkeypatch,mode):
    backend = MenuBackend("daemon",0,**{mode:True})
    result = launch(tmp_path,monkeypatch,backend)
    assert result["state"] == "BLOCKED"
    assert backend.keys == []


class LateMenuBackend(MenuBackend):
    def capture_lines(self, *args, **kwargs):
        if self.captures == 0:
            self.captures += 1
            return ["› Ask Codex to do anything", "? for shortcuts"]
        return super().capture_lines(*args, **kwargs)

def test_boot_composer_must_settle_before_ready(tmp_path,monkeypatch):
    backend=LateMenuBackend("daemon",2)
    result=launch(tmp_path,monkeypatch,backend)
    assert result["state"]=="CREATED"
    assert backend.keys==["Up","Up","Enter"]

class UpdateBackend(MenuBackend):
    def capture_lines(self,*args,**kwargs):
        if self.ready: return ["› Ask Codex to do anything", "codex default"]
        return ["Update available", "› 1. Update now (runs installer)", "2. Skip", "3. Skip until next version", "enter continue · esc skip"]
    def send_keys(self,name,keys):
        assert keys==["Escape"]
        self.keys.extend(keys)
        self.ready=True

def test_skip_exact_update_menu_without_running_installer(tmp_path,monkeypatch):
    backend=UpdateBackend("update",0)
    result=launch(tmp_path,monkeypatch,backend)
    assert result["state"]=="CREATED"
    assert backend.keys==["Escape"]


def test_startup_whitelist_does_not_override_closed_default(tmp_path, monkeypatch):
    config = configured(tmp_path)
    config = replace(config, session_access=replace(config.session_access, default_input=False))
    backend = MenuBackend("daemon", 0)
    result = launch(tmp_path, monkeypatch, backend, config)
    assert result["state"] == "BLOCKED"
    assert backend.keys == []


def test_startup_open_default_does_not_require_retired_whitelist(tmp_path, monkeypatch):
    config = configured(tmp_path)
    config = replace(config, allowed_session_patterns=("other-*",),
                     input_policy=replace(config.input_policy, allowed_session_patterns=("other-*",)))
    backend = MenuBackend("daemon", 0)
    assert launch(tmp_path, monkeypatch, backend, config)["state"] == "CREATED"
    assert backend.keys == ["Enter"]


def test_startup_rechecks_canonical_permission_before_input(tmp_path, monkeypatch):
    backend = MenuBackend("daemon", 0)
    decisions = iter([True, False])
    svc = SessionLifecycleService(configured(tmp_path), backend,
                                  input_authorized=lambda name: next(decisions, False))
    monkeypatch.setattr(svc, "launch_command_for", lambda agent: "codex")
    assert svc.create("codex-nav-check", "codex", str(tmp_path))["state"] == "BLOCKED"
    assert backend.keys == []


TOOL_PERMISSION = [
    'Allow terminal.start_process (osascript Safari)?',
    '› 1. Allow once',
    '  2. Always allow',
    '  3. Cancel',
    'enter to submit | esc to cancel',
    'model: gpt-5',
    'context: 90% left',
    'workspace: /Users/mac17',
    '› Ask Codex to do anything',
    '? for shortcuts',
]


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_tool_permission_dominates_visible_composer(tmp_path, monkeypatch, platform):
    from terminal_mcp.adapters import CodexAdapter, TARGET_WAITING
    from terminal_mcp.lifecycle import _codex_readiness
    from terminal_mcp.status import detect_waiting_input
    monkeypatch.setattr('sys.platform', platform)
    assert CodexAdapter().identify_target_state(TOOL_PERMISSION) == TARGET_WAITING
    assert detect_waiting_input('\n'.join(TOOL_PERMISSION))[0] is True
    assert _codex_readiness(TOOL_PERMISSION) == ('BLOCKED_PERMISSION', 'CODEX_TOOL_PERMISSION_REQUIRED')
    backend = MenuBackend('daemon', 0)
    monkeypatch.setattr(backend, 'capture_lines', lambda *a, **kw: TOOL_PERMISSION)
    result = launch(tmp_path, monkeypatch, backend)
    assert result['state'] == 'BLOCKED_PERMISSION'
    assert result['input_required'] is True
    assert backend.keys == []  # input permission cannot grant external tool/OS rights


def test_composer_alone_does_not_claim_tool_readiness(tmp_path, monkeypatch):
    backend = MenuBackend('daemon', 0)
    backend.ready = True
    result = launch(tmp_path, monkeypatch, backend)
    assert result['state'] == 'CREATED'
    assert result['composer_ready'] is True
    assert result['tool_ready'] is False
    assert result['readiness_stage'] == 'TOOL_PREFLIGHT_REQUIRED'
    assert result['readiness_status'] == 'WAITING_INPUT'


def test_permission_example_in_old_scrollback_does_not_block_composer():
    from terminal_mcp.adapters import CodexAdapter, TARGET_COMPOSER
    from terminal_mcp.status import detect_waiting_input
    lines = TOOL_PERMISSION + ['ordinary output'] * 30 + ['› Ask Codex to do anything']
    assert CodexAdapter().identify_target_state(lines) == TARGET_COMPOSER
    assert detect_waiting_input('\n'.join(lines))[0] is False


def test_old_non_granting_menu_cannot_authorize_keys_in_tool_dialog(tmp_path, monkeypatch):
    from terminal_mcp.lifecycle import _authorized_startup_step
    lines = ['Background server has incompatible feature settings',
             '› 1. Run without daemon this time', '2. Restart with these settings', '3. Cancel'] + TOOL_PERMISSION
    assert _authorized_startup_step(lines) is None
    backend = MenuBackend('daemon', 0)
    monkeypatch.setattr(backend, 'capture_lines', lambda *a, **kw: lines)
    assert launch(tmp_path, monkeypatch, backend)['state'] == 'BLOCKED_PERMISSION'
    assert backend.keys == []


def test_old_update_menu_cannot_dismiss_current_tool_permission():
    from terminal_mcp.lifecycle import _authorized_startup_step
    lines = ['Update now', '2. Skip', '3. Skip until next version', 'esc skip'] + TOOL_PERMISSION
    assert _authorized_startup_step(lines) is None
