from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import pytest

from terminal_mcp.fleet_readiness import FleetReadinessError, prepare_launch, verify_source
from terminal_mcp.launcher_resolution import effective_path


@pytest.mark.parametrize('failure,expected', [
    (FileNotFoundError('git'), 'CLI_UNSUPPORTED'),
    (PermissionError('git'), 'ACL_DENIED'),
    (subprocess.TimeoutExpired(['git'], 8), 'STATUS_PROBE_TIMEOUT'),
])
def test_source_probe_distinguishes_unavailable_denied_and_timeout(monkeypatch, failure, expected):
    from terminal_mcp.fleet_readiness import _git
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(FleetReadinessError, match=expected):
        _git('/unused', 'status')


def test_effective_path_repairs_owned_user_bin(monkeypatch, tmp_path):
    local = tmp_path / '.local/bin'
    local.mkdir(parents=True)
    local.chmod(0o755)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('PATH', '/usr/bin')
    assert str(local) in effective_path().split(':')
    local.chmod(0o777)
    assert str(local) not in effective_path().split(':')


def test_source_pin_rejects_changed_import(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'app').mkdir()
    (tmp_path / 'app/service.py').write_text('value=1\n')
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'fixture'], check=True)
    sha = subprocess.check_output(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], text=True).strip()
    verify_source(str(tmp_path), sha)
    (tmp_path / 'app/service.py').write_text('value=2\n')
    with pytest.raises(FleetReadinessError, match='SOURCE_DIRTY'):
        verify_source(str(tmp_path), sha)


def test_source_pin_rejects_untracked_python(tmp_path):
    test_source_pin_rejects_changed_import(tmp_path)
    subprocess.run(['git', '-C', str(tmp_path), 'checkout', '--', '.'], check=True)
    sha = subprocess.check_output(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], text=True).strip()
    (tmp_path / 'app/untracked.py').write_text('pass')
    with pytest.raises(FleetReadinessError, match='SOURCE_DIRTY'):
        verify_source(str(tmp_path), sha)


def test_disabled_profile_does_not_migrate_any_launch():
    assert prepare_launch({}, cwd='/tmp', agent='codex', prompt='hi', session='test', binding='task') is None


def _service(tmp_path, monkeypatch, *, input_allowed=True, send_allowed=True):
    from terminal_mcp.config import AppConfig, PermissionsConfig, SessionLifecycleConfig, InputPolicyConfig
    from terminal_mcp.core import TerminalService
    config = AppConfig(permissions=PermissionsConfig(True, input_allowed), allowed_session_patterns=("fleet-*",),
        input_policy=InputPolicyConfig(allow_send_text=send_allowed),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),),
            fleet_readiness={'enabled': True, 'workspaces': [{'workspace': str(tmp_path), 'binding': 'fleet-task'}]}))
    service = TerminalService(config)
    monkeypatch.setattr(service, '_session_capacity', lambda: (0, 0))
    monkeypatch.setattr(service.lifecycle, 'create', lambda *a, **k: pytest.fail('spawn before guard'))
    return service


@pytest.mark.parametrize('input_allowed,send_allowed,grant,error', [
    (False, True, 'read_send', 'INPUT_DISABLED'),
    (True, False, 'read_send', 'ACTION_NOT_ALLOWED'),
    (True, True, 'none', 'READ_SEND_REQUIRED'),
])
def test_real_create_guard_blocks_before_any_preflight_or_spawn(tmp_path, monkeypatch, input_allowed, send_allowed, grant, error):
    service = _service(tmp_path, monkeypatch, input_allowed=input_allowed, send_allowed=send_allowed)
    monkeypatch.setattr('terminal_mcp.fleet_readiness.prepare_launch', lambda *a, **k: pytest.fail('preflight before guard'))
    result = service.terminal_create_session('fleet-task', 'codex', str(tmp_path), initial_prompt='write canary', grant_mode=grant, binding='fleet-task')
    assert result['error'] == error


def test_actual_create_uses_shared_runner_and_never_types_prompt_twice(tmp_path, monkeypatch):
    from terminal_mcp.fleet_readiness import PreparedFleetLaunch
    service = _service(tmp_path, monkeypatch)
    prepared = PreparedFleetLaunch((sys.executable, '-I', '/pinned/fleet-permissions.py'), 'req', {'preflight': {'code': 'PASS'}})
    monkeypatch.setattr('terminal_mcp.fleet_readiness.prepare_launch', lambda *a, **k: prepared)
    seen = []
    def create(*args, **kwargs):
        seen.append(kwargs)
        return {'session': 'fleet-task', 'state': 'SUBMITTED', 'cwd': str(tmp_path)}
    monkeypatch.setattr(service.lifecycle, 'create', create)
    monkeypatch.setattr(service, 'grant_session_read', lambda *a, **k: {'read_enabled': True})
    monkeypatch.setattr(service, 'grant_session_input', lambda *a, **k: {'input_enabled': True})
    monkeypatch.setattr(service, 'terminal_bind', lambda *a, **k: {'binding': 'fleet-task', 'session': 'fleet-task'})
    monkeypatch.setattr(service, '_input_authorized', lambda *a: (True, None))
    monkeypatch.setattr('terminal_mcp.fleet_readiness.release_dispatch_gate', lambda *a, **k: None)
    monkeypatch.setattr(service, 'terminal_send_text', lambda *a, **k: pytest.fail('duplicate prompt'))
    monkeypatch.setattr(service, '_start_knowledge_capture_for_new_session', lambda *a: None)
    result = service.terminal_create_session('fleet-task', 'codex', str(tmp_path), initial_prompt='write canary', grant_mode='read_send', binding='fleet-task')
    assert seen[0]['prepared_fleet'] == prepared
    assert result['state'] == 'SUBMITTED'
    assert result['initial_prompt_result']['execution_confirmed'] is False


def test_preflight_timeout_is_not_permission_denial(monkeypatch):
    from terminal_mcp.fleet_readiness import _run_json
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('probe', 1)))
    with pytest.raises(FleetReadinessError, match='STATUS_PROBE_TIMEOUT'):
        _run_json(['test'], timeout=1)


def test_installer_readiness_mode_fails_without_touching_identity(tmp_path):
    import json
    config = tmp_path / 'readiness.json'
    config.write_text(json.dumps({'workspaces': []}))
    repo = Path(__file__).resolve().parents[1]
    for installer in ('install-node-agent.sh', 'install-node-agent-macos.sh'):
        result = subprocess.run(['bash', str(repo / 'deploy' / installer), '--fleet-readiness-config', str(config),
            '--fleet-workspace', str(tmp_path), '--repo-dir', str(tmp_path / 'absent')],
            capture_output=True, text=True, timeout=10,
            env={**__import__('os').environ, 'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin'})
        assert result.returncode == 1
        assert json.loads(result.stdout)['code'] == 'TRUST_REQUIRED'
        assert not (tmp_path / 'absent').exists()


def test_repeated_task_key_reuses_prompt_and_rejects_changed_prompt(tmp_path, monkeypatch):
    from terminal_mcp import fleet_readiness as module
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    config = {'enabled': True, 'source_sha': 'a' * 40, 'workspaces': [{
        'workspace': str(tmp_path), 'state': str(state), 'repo': 'owner/repo', 'binding': 'task', 'task_id': 't1'}]}
    monkeypatch.setattr(module, 'validate_project_directory', lambda *a, **k: None)
    monkeypatch.setattr(module, 'runner_argv', lambda config, entry, action: [sys.executable, action])
    monkeypatch.setattr(module, '_run_json', lambda *a, **k: {'code': 'PASS'})
    first = module.prepare_launch(config, cwd=str(tmp_path), agent='codex', prompt='one', session='s1', binding='task')
    repeat = module.prepare_launch(config, cwd=str(tmp_path), agent='codex', prompt='one', session='s2', binding='task')
    assert first.request_key == repeat.request_key
    assert len(list(state.glob('*.prompt'))) == 1
    with pytest.raises(FleetReadinessError, match='REQUEST_KEY_CONFLICT'):
        module.prepare_launch(config, cwd=str(tmp_path), agent='codex', prompt='two', session='s2', binding='task')


def test_failed_preflight_does_not_write_or_spawn(tmp_path, monkeypatch):
    from terminal_mcp import fleet_readiness as module
    config = {'enabled': True, 'workspaces': [{'workspace': str(tmp_path), 'binding': 'task', 'task_id': 't1'}]}
    monkeypatch.setattr(module, 'validate_project_directory', lambda *a, **k: None)
    monkeypatch.setattr(module, 'runner_argv', lambda config, entry, action: ['preflight'])
    monkeypatch.setattr(module, '_run_json', lambda *a, **k: {'code': 'BLOCKED', 'failures': {'gh_identity': 'AUTH_REQUIRED'}})
    result = module.prepare_launch(config, cwd=str(tmp_path), agent='codex', prompt='one', session='s1', binding='task')
    assert result.argv == ()
    assert result.receipt['preflight']['failures'] == {'gh_identity': 'AUTH_REQUIRED'}
    assert list(tmp_path.iterdir()) == []


def test_reused_policy_checks_exact_canonical_repository(tmp_path):
    from terminal_mcp.agent_launch_policy import validate_project_directory, LaunchPolicyError
    repository = tmp_path / 'repo'
    repository.mkdir()
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    assert validate_project_directory(repository, allowed_roots=[str(tmp_path)],
        trusted_project_roots=[str(repository)]).canonical_repo_root == repository
    with pytest.raises(LaunchPolicyError, match='PROJECT_ROOT_NOT_TRUSTED'):
        validate_project_directory(repository, allowed_roots=[str(tmp_path)], trusted_project_roots=[str(tmp_path)])


def test_gate_is_not_released_when_actual_binding_fails(tmp_path, monkeypatch):
    from terminal_mcp.fleet_readiness import PreparedFleetLaunch
    service = _service(tmp_path, monkeypatch)
    prepared = PreparedFleetLaunch((sys.executable, '-I', '/pinned/fleet-permissions.py'), 'req', {}, tmp_path/'gate')
    monkeypatch.setattr('terminal_mcp.fleet_readiness.prepare_launch', lambda *a, **k: prepared)
    monkeypatch.setattr(service.lifecycle, 'create', lambda *a, **k: {'session': 'fleet-task', 'state': 'SUBMITTED', 'cwd': str(tmp_path)})
    monkeypatch.setattr(service, 'grant_session_read', lambda *a, **k: {'read_enabled': True})
    monkeypatch.setattr(service, 'grant_session_input', lambda *a, **k: {'input_enabled': True})
    monkeypatch.setattr(service, 'terminal_bind', lambda *a, **k: {'error': 'BINDING_EXISTS'})
    monkeypatch.setattr(service, '_start_knowledge_capture_for_new_session', lambda *a: None)
    monkeypatch.setattr('terminal_mcp.fleet_readiness.release_dispatch_gate', lambda *a, **k: pytest.fail('grant race released gate'))
    result = service.terminal_create_session('fleet-task', 'codex', str(tmp_path), initial_prompt='write canary', grant_mode='read_send', binding='fleet-task')
    assert result['error'] == 'GRANT_HANDOFF_DENIED'
    assert result['initial_prompt_result']['sent'] is False
    assert not prepared.gate_path.exists()


def test_gate_private_receipt_only_after_explicit_release(tmp_path):
    import json
    from terminal_mcp.fleet_readiness import PreparedFleetLaunch, release_dispatch_gate
    prepared = PreparedFleetLaunch(('python',), 'req', {}, tmp_path/'gate')
    assert not prepared.gate_path.exists()
    release_dispatch_gate(prepared, workspace=tmp_path, session='s1', binding='b1')
    receipt = json.loads(prepared.gate_path.read_text())
    assert receipt['approved'] is True
    assert receipt['request_key'] == 'req'
    assert prepared.gate_path.stat().st_mode & 0o077 == 0


def test_source_pin_checks_assume_unchanged_bytes(tmp_path):
    test_source_pin_rejects_changed_import(tmp_path)
    subprocess.run(['git', '-C', str(tmp_path), 'checkout', '--', '.'], check=True)
    sha = subprocess.check_output(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', '-C', str(tmp_path), 'update-index', '--assume-unchanged', 'app/service.py'], check=True)
    (tmp_path/'app/service.py').write_text('value=9\n')
    with pytest.raises(FleetReadinessError, match='SOURCE_BLOB_MISMATCH'):
        verify_source(str(tmp_path), sha)


def test_pinned_runner_cannot_import_poisoned_source_pycache(tmp_path):
    import os
    import py_compile
    from terminal_mcp.fleet_readiness import runner_argv, _run_json
    source = tmp_path/'source'
    source.mkdir(mode=0o755)
    (source/'app').mkdir()
    (source/'scripts').mkdir()
    module = source/'app/service.py'
    original = 'value="SAFE"\n'
    module.write_text(original)
    (source/'.gitignore').write_text('__pycache__/\n')
    (source/'scripts/fleet-permissions.py').write_text('import sys,json\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]))\nfrom app.service import value\nprint(json.dumps({"code":"PASS","value":value}))\n')
    subprocess.run(['git','init','-q',str(source)],check=True)
    subprocess.run(['git','-C',str(source),'add','.'],check=True)
    subprocess.run(['git','-C',str(source),'-c','user.name=Test','-c','user.email=t@local','commit','-qm','fixture'],check=True)
    sha = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    stamp = module.stat()
    module.write_text('value="EVIL"\n')
    os.utime(module, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    py_compile.compile(str(module), doraise=True)
    module.write_text(original)
    os.utime(module, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    config={'source':str(source),'source_sha':sha}
    entry={'workspace':str(tmp_path),'state':str(tmp_path/'state'),'repo':'owner/repo'}
    report = _run_json(runner_argv(config,entry,'audit'), timeout=5)
    assert report['value'] == 'SAFE'


def test_source_pin_rejects_ignored_root_stdlib_shadow(tmp_path):
    test_source_pin_rejects_changed_import(tmp_path)
    subprocess.run(['git', '-C', str(tmp_path), 'checkout', '--', '.'], check=True)
    sha = subprocess.check_output(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], text=True).strip()
    (tmp_path/'.git/info/exclude').write_text('subprocess.py\n')
    (tmp_path/'subprocess.py').write_text('raise RuntimeError("shadow")\n')
    with pytest.raises(FleetReadinessError, match='SOURCE_DIRTY'):
        verify_source(str(tmp_path), sha)


def test_source_pin_verifies_hidden_policy_symlink_without_following(tmp_path):
    test_source_pin_rejects_changed_import(tmp_path)
    subprocess.run(['git', '-C', str(tmp_path), 'checkout', '--', '.'], check=True)
    hidden = tmp_path/'.claude/skills'
    hidden.mkdir(parents=True)
    link = hidden/'known'
    link.symlink_to('/outside/skill-that-is-not-imported')
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.claude'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test', '-c', 'user.email=t@local', 'commit', '-qm', 'policy link'], check=True)
    sha = subprocess.check_output(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], text=True).strip()
    verify_source(str(tmp_path), sha)
    subprocess.run(['git', '-C', str(tmp_path), 'update-index', '--assume-unchanged', '.claude/skills/known'], check=True)
    link.unlink()
    link.symlink_to('/changed/policy')
    with pytest.raises(FleetReadinessError, match='SOURCE_BLOB_MISMATCH'):
        verify_source(str(tmp_path), sha)
