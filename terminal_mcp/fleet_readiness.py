"""Explicit trusted-task adapter to ProjectFlow's single versioned runner.

No copied readiness database, credential handling, CLI flags or trust-dialog
answers. Server configuration selects exact workspaces; ordinary sessions keep
existing behavior. The pinned source is verified including its imported code.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import tempfile
from dataclasses import dataclass

from .agent_launch_policy import LaunchPolicyError, validate_project_directory
from .launcher_resolution import effective_path


class FleetReadinessError(ValueError):
    pass


def _git(source, *args):
    try:
        result = subprocess.run(['git', '-C', str(source), *args], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=8,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    except subprocess.TimeoutExpired as exc:
        raise FleetReadinessError('STATUS_PROBE_TIMEOUT') from exc
    except PermissionError as exc:
        raise FleetReadinessError('ACL_DENIED') from exc
    except OSError as exc:
        raise FleetReadinessError('CLI_UNSUPPORTED') from exc
    if result.returncode:
        raise FleetReadinessError('SOURCE_UNVERIFIED')
    return result.stdout.strip()


def verify_source(source: str, sha: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{40}', sha or ''):
        raise FleetReadinessError('SOURCE_PIN_REQUIRED')
    root = Path(source).expanduser().resolve(strict=True)
    if hasattr(os, 'getuid') and (root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o022):
        raise FleetReadinessError('SOURCE_NOT_OWNED')
    if _git(root, 'rev-parse', 'HEAD') != sha:
        raise FleetReadinessError('SOURCE_SHA_MISMATCH')
    if _git(root, 'status', '--porcelain', '--untracked-files=all'):
        raise FleetReadinessError('SOURCE_DIRTY')
    # Ignored Python modules can shadow tracked imports despite a clean status.
    tracked = set()
    # Read the pinned commit tree, not index flags or git status alone:
    # assume-unchanged/skip-worktree must not hide modified imported code.
    tree = _git(root, 'ls-tree', '-rz', '--full-tree', sha)
    for record in tree.split('\0'):
        if not record:
            continue
        metadata, name = record.split('\t', 1)
        mode, kind, blob = metadata.split()
        path = root / name
        hidden = any(part.startswith('.') for part in Path(name).parts)
        if kind == 'blob' and mode == '120000' and hidden and path.is_symlink():
            # Tracked hidden policy links are not on Python's import surface.
            # Verify their literal committed link bytes without following them.
            data = os.fsencode(os.readlink(path))
        elif kind == 'blob' and mode in ('100644', '100755') and not path.is_symlink() and path.is_file():
            data = path.read_bytes()
        else:
            raise FleetReadinessError('SOURCE_BLOB_MISMATCH')
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if actual != blob:
            raise FleetReadinessError('SOURCE_BLOB_MISMATCH')
        tracked.add(name)
    # The entry point puts the repository root on sys.path, so ignored
    # root modules (json.py, subprocess.py, etc.) and namespace packages
    # also belong to the import surface. Hidden venv/Git dirs are not
    # importable root package names and are intentionally excluded.
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not name.startswith('.') and name != '__pycache__']
        for name in [*directories, *files]:
            path = Path(parent) / name
            if path.is_symlink() or (path.suffix in ('.py', '.pyc', '.so', '.pyd', '.pth')
                                    and str(path.relative_to(root)) not in tracked):
                raise FleetReadinessError('SOURCE_DIRTY')
    return root


def selected_entry(config, cwd):
    if not config.get('enabled') or not cwd:
        return None
    resolved = str(Path(cwd).expanduser().resolve())
    entries = [entry for entry in config.get('workspaces', ())
               if str(Path(entry['workspace']).expanduser().resolve()) == resolved]
    if len(entries) > 1:
        raise FleetReadinessError('DUPLICATE_WORKSPACE_POLICY')
    return entries[0] if entries else None


@dataclass(frozen=True)
class PreparedFleetLaunch:
    argv: tuple[str, ...]
    request_key: str
    receipt: dict
    gate_path: Path | None = None


def runner_argv(config, entry, action):
    root = verify_source(config['source'], config['source_sha'])
    script = root / 'scripts/fleet-permissions.py'
    if not script.is_file():
        raise FleetReadinessError('SOURCE_UNVERIFIED')
    python = Path(config.get('python', sys.executable)).expanduser().absolute()
    if not python.is_file():
        raise FleetReadinessError('CLI_UNSUPPORTED')
    # -I does not disable source-tree pyc caches. A fresh private cache
    # prefix plus -B ensures every imported project module reads verified
    # source, even if an ignored timestamp-valid __pycache__ was poisoned.
    cache = tempfile.mkdtemp(prefix='pf-fleet-pycache-')
    args = [str(python), '-I', '-B', '-X', 'pycache_prefix=' + cache,
            str(script), action, '--workspace', entry['workspace'],
            '--state', entry['state'], '--repo', entry['repo']]
    return args


def run_action(config, entry, action, *, authorize_apply=False):
    if action not in ('audit', 'apply', 'check', 'rollback'):
        raise FleetReadinessError('INVALID_READINESS_ACTION')
    argv = runner_argv(config, entry, action)
    if action == 'apply':
        if not authorize_apply:
            raise FleetReadinessError('TRUST_REQUIRED')
        argv.append('--trust-owned-workspace')
    return _run_json(argv, timeout=90)


def _run_json(argv, *, timeout):
    env = {key: value for key, value in os.environ.items() if key not in ('PYTHONPATH', 'PYTHONHOME')}
    env['PATH'] = effective_path()
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise FleetReadinessError('STATUS_PROBE_TIMEOUT') from exc
    except OSError as exc:
        raise FleetReadinessError('CLI_UNSUPPORTED') from exc
    finally:
        # Only our empty per-probe directory; never recursive deletion.
        for argument in argv:
            if argument.startswith('pycache_prefix='):
                cache = Path(argument.split('=', 1)[1])
                if cache.name.startswith('pf-fleet-pycache-'):
                    try:
                        cache.rmdir()
                    except OSError:
                        pass
    try:
        report = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise FleetReadinessError('EXECUTION_UNKNOWN') from exc
    if not isinstance(report, dict):
        raise FleetReadinessError('EXECUTION_UNKNOWN')
    if result.returncode and report.get('code') == 'PASS':
        raise FleetReadinessError('EXECUTION_UNKNOWN')
    return report


def prepare_launch(config, *, cwd, agent, prompt, session, binding, allowed_roots=()):
    entry = selected_entry(config, cwd)
    if entry is None:
        return None
    if (agent not in ('codex', 'claude') or not prompt or not binding or binding != entry.get('binding')
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', entry.get('task_id', ''))):
        raise FleetReadinessError('TRUST_REQUIRED')
    try:
        validate_project_directory(cwd, allowed_roots=allowed_roots,
                                   trusted_project_roots=config.get('trusted_project_roots', ()))
    except LaunchPolicyError as exc:
        raise FleetReadinessError(exc.code) from exc
    args = runner_argv(config, entry, 'preflight')
    options = ['--agent', agent, '--scope', entry.get('scope', 'local'),
               '--runtime', entry.get('runtime', 'native')]
    if entry.get('image'):
        options += ['--image', entry['image']]
    report = _run_json([*args, *options], timeout=90)
    if report.get('code') != 'PASS':
        return PreparedFleetLaunch((), '', {'error': report.get('code', 'EXECUTION_UNKNOWN'),
                                            'preflight': report, 'state': 'BLOCKED'})
    # Stable across retries and service restarts; runner owns durable lease/dedupe.
    key = 'mcp-' + hashlib.sha256(json.dumps([entry['workspace'], binding, entry['task_id']],
                                           ensure_ascii=False).encode()).hexdigest()
    state = Path(entry['state']).expanduser().resolve(strict=True)
    if hasattr(os, 'getuid') and (state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077):
        raise FleetReadinessError('ACL_DENIED')
    prompt_file = state / (key + '.prompt')
    try:
        fd = os.open(prompt_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if prompt_file.is_symlink() or prompt_file.read_text() != prompt:
            raise FleetReadinessError('REQUEST_KEY_CONFLICT')
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(prompt)
            stream.flush()
            os.fsync(stream.fileno())
    gate_path = state / (key + '.' + uuid.uuid4().hex + '.gate.json')
    argv = [*runner_argv(config, entry, 'launch'), *options,
            '--request-key', key, '--prompt-file', str(prompt_file), '--dispatch-gate', str(gate_path)]
    return PreparedFleetLaunch(tuple(argv), key, {'schema_version': 1, 'source_sha': config['source_sha'],
                                                 'request_key': key, 'task_id': entry['task_id'], 'preflight': report}, gate_path)



def release_dispatch_gate(prepared, *, workspace, session, binding):
    """Called only after the existing grant/binding methods report success."""
    if prepared.gate_path is None:
        raise FleetReadinessError('TRUST_REQUIRED')
    now = time.time()
    receipt = {'approved': True, 'request_key': prepared.request_key,
               'workspace': str(Path(workspace).resolve()), 'session': session,
               'binding': binding, 'created_at': now, 'expires_at': now + 20}
    fd, temporary = tempfile.mkstemp(prefix='.dispatch-gate-', dir=prepared.gate_path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(receipt, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, prepared.gate_path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv=None):
    """Existing installers' readiness-only mode; defaults to audit, no restart."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--action', choices=('audit', 'apply', 'check', 'rollback'), default='audit')
    parser.add_argument('--authorize-trust', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text())
        entry = selected_entry({**config, 'enabled': True}, args.workspace)
        if entry is None:
            raise FleetReadinessError('TRUST_REQUIRED')
        report = run_action(config, entry, args.action, authorize_apply=args.authorize_trust)
    except (OSError, ValueError, KeyError) as exc:
        report = {'code': str(exc) if isinstance(exc, FleetReadinessError) else 'TRUST_REQUIRED',
                  'execution': 'NOT_DISPATCHED'}
    print(json.dumps(report, indent=2))
    return 0 if report.get('code', 'PASS') == 'PASS' else 1
