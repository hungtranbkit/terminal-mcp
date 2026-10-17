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
        self.launch_args = None
        self.replace_pane, self.drift = replace_pane, drift
    def get_session(self, name):
        if not self.exists: return None
        pane = "%2" if self.replace_pane and self.captures else "%1"
        return SimpleNamespace(session_id="$1", pane_id=pane, created_epoch=1, pane_dead=False, pane_current_command="codex")
    def new_session(self, *args, **kwargs):
        self.exists = True
        self.launch_args = kwargs.get("extra_args")
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
    assert result["state"] == "READY"
    assert backend.keys == expected

@pytest.mark.parametrize("denial",["pattern"])
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
    assert result["blocked_reason"] == "CODEX_STARTUP_MENU"
    assert result["startup_prompt_id"] == "codex.daemon_feature_mismatch.v1"
    assert "option 1" in result["next_action"]
    assert backend.keys == []


def test_exact_daemon_fallback_is_safe_without_general_terminal_input(tmp_path, monkeypatch):
    config = configured(tmp_path)
    config = replace(config, permissions=replace(config.permissions, terminal_input=False, allow_send_keys=False))
    config = replace(config, input_policy=replace(config.input_policy, allow_keys=()))
    backend = MenuBackend("daemon", 2)
    result = launch(tmp_path, monkeypatch, backend, config)
    assert result["state"] == "READY"
    assert backend.keys == ["Up", "Up", "Enter"]

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
    assert result["state"]=="READY"
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
    assert result["state"]=="READY"
    assert backend.keys==["Escape"]


def test_scoped_safe_daemon_choice_is_not_general_session_input(tmp_path, monkeypatch):
    config = configured(tmp_path)
    config = replace(config, session_access=replace(config.session_access, default_input=False))
    backend = MenuBackend("daemon", 0)
    result = launch(tmp_path, monkeypatch, backend, config)
    assert result["state"] == "READY"
    assert backend.keys == ["Enter"]


def test_startup_open_default_does_not_require_retired_whitelist(tmp_path, monkeypatch):
    config = configured(tmp_path)
    config = replace(config, allowed_session_patterns=("other-*",),
                     input_policy=replace(config.input_policy, allowed_session_patterns=("other-*",)))
    backend = MenuBackend("daemon", 0)
    assert launch(tmp_path, monkeypatch, backend, config)["state"] == "READY"
    assert backend.keys == ["Enter"]


def test_scoped_safe_daemon_choice_does_not_require_general_input_grant(tmp_path, monkeypatch):
    backend = MenuBackend("daemon", 0)
    decisions = iter([True, False])
    svc = SessionLifecycleService(configured(tmp_path), backend,
                                  input_authorized=lambda name: next(decisions, False))
    monkeypatch.setattr(svc, "launch_command_for", lambda agent: "codex")
    assert svc.create("codex-nav-check", "codex", str(tmp_path))["state"] == "READY"
    assert backend.keys == ["Enter"]


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
@pytest.mark.parametrize('argument_lines', [0, 17, 100])
def test_tool_permission_dominates_visible_composer(tmp_path, monkeypatch, platform, argument_lines):
    from terminal_mcp.adapters import CodexAdapter, TARGET_WAITING
    from terminal_mcp.lifecycle import _codex_readiness
    from terminal_mcp.status import detect_waiting_input
    monkeypatch.setattr('sys.platform', platform)
    lines = TOOL_PERMISSION[:1] + ['tell application Safari -- wrapped argument'] * argument_lines + TOOL_PERMISSION[1:]
    assert CodexAdapter().identify_target_state(lines) == TARGET_WAITING
    assert detect_waiting_input('\n'.join(lines))[0] is True
    assert _codex_readiness(lines) == ('BLOCKED_PERMISSION', 'CODEX_TOOL_PERMISSION_REQUIRED')
    backend = MenuBackend('daemon', 0)
    monkeypatch.setattr(backend, 'capture_lines', lambda *a, **kw: lines)
    result = launch(tmp_path, monkeypatch, backend)
    assert result['state'] == 'BLOCKED_PERMISSION'
    assert result['input_required'] is True
    assert backend.keys == []  # input permission cannot grant external tool/OS rights


def test_composer_alone_does_not_claim_tool_readiness(tmp_path, monkeypatch):
    backend = MenuBackend('daemon', 0)
    backend.ready = True
    result = launch(tmp_path, monkeypatch, backend)
    assert result['state'] == 'READY'
    assert result['composer_ready'] is True
    assert result['tool_ready'] is False
    assert result['readiness_stage'] == 'TASK_READY'
    assert result['readiness_status'] == 'READY'


def test_exact_incompatible_daemon_prompt_is_classified_and_selects_only_run_once(tmp_path, monkeypatch):
    from terminal_mcp.lifecycle import _authorized_startup_step, _codex_readiness
    lines = [
        "Background server has incompatible feature settings",
        "This session requires api_key_model_discovery to be enabled",
        "Restart will use shared feature settings",
        "› 1. Run without daemon this time",
        "  2. Restart with these settings",
        "  3. Cancel",
    ]
    assert _codex_readiness(lines) == ("BLOCKED", "CODEX_STARTUP_MENU")
    assert _authorized_startup_step(lines) == ("CODEX_STARTUP_MENU", "Enter")
    backend = MenuBackend("daemon", 0)
    monkeypatch.setattr(backend, "capture_lines", lambda *a, **kw: lines if not backend.ready else ["› Ask Codex to do anything", "codex default"])
    result = launch(tmp_path, monkeypatch, backend)
    assert result["composer_ready"] is True
    assert backend.keys == ["Enter"]


def test_old_daemon_menu_in_scrollback_does_not_block_current_composer():
    from terminal_mcp.lifecycle import _authorized_startup_step, _codex_readiness
    lines = [
        "Background server has incompatible feature settings",
        "This session requires api_key_model_discovery to be enabled",
        "› 1. Run without daemon this time",
        "2. Restart with these settings",
        "3. Cancel",
        *(["ordinary Codex output"] * 20),
        "› Ask Codex to do anything",
        "codex default",
    ]
    assert _codex_readiness(lines) == ("READY", None)
    assert _authorized_startup_step(lines) is None


@pytest.mark.parametrize("version,supports", [("0.154.0", False), ("0.162.0", True)])
def test_codex_no_daemon_flag_is_detected_from_cli_help(monkeypatch, version, supports):
    from terminal_mcp.lifecycle import _codex_supports_no_daemon
    _codex_supports_no_daemon.cache_clear()
    class HelpResult:
        stdout = f"codex-cli {version}\n" + ("--no-daemon Run without the shared background server\n" if supports else "--help Show help\n")
        stderr = ""
        returncode = 0
    monkeypatch.setattr("terminal_mcp.lifecycle.subprocess.run", lambda *a, **kw: HelpResult())
    assert _codex_supports_no_daemon("/bin/codex") is supports


def test_supported_codex_gets_scoped_no_daemon_before_resume_args(tmp_path, monkeypatch):
    from terminal_mcp.lifecycle import _codex_supports_no_daemon
    _codex_supports_no_daemon.cache_clear()
    monkeypatch.setattr("terminal_mcp.lifecycle.subprocess.run", lambda *a, **kw: SimpleNamespace(
        stdout="--no-daemon Run without shared background server", stderr="", returncode=0))
    backend = MenuBackend("daemon", 0)
    backend.ready = True
    svc = SessionLifecycleService(configured(tmp_path), backend)
    monkeypatch.setattr(svc, "launch_command_for", lambda agent: "/bin/codex")
    result = svc.create("codex-nav-check", "codex", str(tmp_path), extra_args=("--resume", "session-id"))
    assert result["state"] == "READY"
    assert backend.launch_args == ("--no-daemon", "--resume", "session-id")


def test_untrusted_folder_prompt_stays_blocked_even_when_cli_has_no_daemon(tmp_path, monkeypatch):
    from terminal_mcp.lifecycle import _codex_readiness
    assert _codex_readiness(FOLDER_TRUST_MENU) == ("BLOCKED", "CODEX_TRUST_PROMPT")
    backend = MenuBackend("daemon", 0)
    monkeypatch.setattr(backend, "capture_lines", lambda *a, **kw: FOLDER_TRUST_MENU)
    result = launch(tmp_path, monkeypatch, backend)
    assert result["state"] == "BLOCKED"
    assert backend.keys == []


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


def test_tool_permission_header_clipped_by_capture_still_blocks():
    from terminal_mcp.adapters import CodexAdapter, TARGET_WAITING
    from terminal_mcp.lifecycle import _codex_readiness, _authorized_startup_step
    lines = TOOL_PERMISSION[1:]
    assert CodexAdapter().identify_target_state(lines) == TARGET_WAITING
    assert _codex_readiness(lines)[0] == "BLOCKED_PERMISSION"
    assert _authorized_startup_step(lines) is None


FOLDER_TRUST_MENU = [
    "Folder settings can run code automatically",
    "Trust and continue",
    "Quit",
    "› Ask Codex to do anything",
]


def test_codex_folder_trust_prompt_dominates_visible_composer_and_is_not_auto_accepted():
    from terminal_mcp.adapters import CodexAdapter, TARGET_WAITING
    from terminal_mcp.lifecycle import _codex_readiness, _authorized_startup_step
    from terminal_mcp.status import detect_waiting_input

    assert CodexAdapter().identify_target_state(FOLDER_TRUST_MENU) == TARGET_WAITING
    assert detect_waiting_input("\n".join(FOLDER_TRUST_MENU))[0] is True
    assert _codex_readiness(FOLDER_TRUST_MENU) == ("BLOCKED", "CODEX_TRUST_PROMPT")
    assert _authorized_startup_step(FOLDER_TRUST_MENU) is None


def test_old_folder_trust_dialog_in_scrollback_does_not_block_composer():
    from terminal_mcp.adapters import CodexAdapter, TARGET_COMPOSER
    from terminal_mcp.lifecycle import _codex_readiness

    lines = FOLDER_TRUST_MENU[:3] + ["ordinary output"] * 20 + ["› Ask Codex to do anything"]
    assert CodexAdapter().identify_target_state(lines) == TARGET_COMPOSER
    assert _codex_readiness(lines) == ("READY", None)
