"""terminal_put_file: write a file straight onto a node instead of pushing
base64 through a tmux pane.

The pane route was measured on 2026-09-21 at ~43 KB/s with one "Called tool"
row per 18 KB, and a chunk over ~24,000 characters is truncated by tmux
SILENTLY while `base64 -d` still exits 0 -- a valid file with wrong contents.
Every guard below exists because that class of silent-wrong-answer is the
failure this path must not reproduce.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import os

import pytest

from terminal_mcp.config import load_config
from terminal_mcp.core import TerminalService


@pytest.fixture
def service(tmp_path):
    base = load_config()
    lifecycle = dataclasses.replace(
        base.session_lifecycle, allow_put_file=True,
        allowed_cwd_roots=(str(tmp_path),), max_put_file_bytes=1024)
    return TerminalService(dataclasses.replace(base, session_lifecycle=lifecycle)), tmp_path


@pytest.fixture
def payload():
    blob = os.urandom(512)
    return blob, base64.b64encode(blob).decode(), hashlib.sha256(blob).hexdigest()


def test_refused_unless_the_host_opted_in(tmp_path, monkeypatch):
    # Writing an arbitrary file is a real capability: upgrading must not grant
    # it. Pinned to False explicitly rather than inherited from load_config(),
    # because a deployment that HAS opted in would otherwise make this test
    # pass or fail depending on the machine it runs on -- which is how the
    # assertion stopped meaning anything on dell once put_file was enabled there.
    monkeypatch.delenv("TERMINAL_MCP_ALLOW_PUT_FILE", raising=False)
    base = load_config()
    lifecycle = dataclasses.replace(base.session_lifecycle, allow_put_file=False,
                                    allowed_cwd_roots=(str(tmp_path),))
    svc = TerminalService(dataclasses.replace(base, session_lifecycle=lifecycle))
    assert svc.terminal_put_file(str(tmp_path / "a.bin"), "AAAA")["error"] == "PUT_FILE_DISABLED"


def test_writes_the_exact_bytes_and_reports_a_matching_digest(service, payload):
    svc, root = service
    blob, b64, digest = payload
    result = svc.terminal_put_file(str(root / "a.bin"), b64)
    assert result["sha256"] == digest and result["bytes"] == len(blob)
    assert result["overwritten"] is False
    # The digest is only useful if it describes what actually landed on disk.
    assert (root / "a.bin").read_bytes() == blob


def test_existing_file_is_kept_unless_overwrite_is_asked_for(service, payload):
    svc, root = service
    _, b64, _ = payload
    svc.terminal_put_file(str(root / "a.bin"), b64)
    assert svc.terminal_put_file(str(root / "a.bin"), b64)["error"] == "FILE_EXISTS"
    assert svc.terminal_put_file(str(root / "a.bin"), b64, overwrite=True)["overwritten"] is True


@pytest.mark.parametrize("path, expected", [
    ("/etc/evil.bin", "CWD_NOT_ALLOWED"),          # outside allowed_cwd_roots
    ("relative.bin", "INVALID_PATH"),              # not absolute
])
def test_destination_is_gated(service, payload, path, expected):
    svc, _ = service
    _, b64, _ = payload
    assert svc.terminal_put_file(path, b64)["error"] == expected


def test_dot_dot_cannot_walk_out_of_an_allowed_root(service, payload):
    # The property under test is that the write is REFUSED, not which refusal
    # code comes back: `..` resolves before the containment check, so the
    # escaped path is rejected as outside the roots when it exists and as
    # missing when it does not. Both are the gate doing its job.
    svc, root = service
    _, b64, _ = payload
    result = svc.terminal_put_file(f"{root}/../../etc/evil.bin", b64)
    assert result["error"] in {"CWD_NOT_ALLOWED", "CWD_NOT_FOUND"}
    assert not (root.parent.parent / "etc" / "evil.bin").exists()


def test_an_existing_symlink_is_refused_not_followed(service, payload):
    # Otherwise a planted link redirects the write past a gate that has passed.
    svc, root = service
    _, b64, _ = payload
    victim = root / "victim.txt"
    victim.write_text("original")
    os.symlink(victim, root / "link.bin")
    assert svc.terminal_put_file(str(root / "link.bin"), b64, overwrite=True)["error"] == "PATH_IS_SYMLINK"
    assert victim.read_text() == "original"


def test_malformed_base64_is_an_error_never_silently_skipped(service):
    # validate=True matters: quietly dropping bad characters is exactly how the
    # tmux route produced a valid file with the wrong bytes.
    svc, root = service
    assert svc.terminal_put_file(str(root / "b.bin"), "not!valid!base64")["error"] == "INVALID_BASE64"


def test_payload_over_the_cap_is_refused(service):
    svc, root = service
    oversized = base64.b64encode(os.urandom(4096)).decode()
    assert svc.terminal_put_file(str(root / "c.bin"), oversized)["error"] == "FILE_TOO_LARGE"


def test_mode_is_applied_and_validated(service, payload):
    svc, root = service
    _, b64, _ = payload
    assert svc.terminal_put_file(str(root / "d.bin"), b64, mode="rwx")["error"] == "INVALID_MODE"
    svc.terminal_put_file(str(root / "e.bin"), b64, mode="600")
    assert oct(os.stat(root / "e.bin").st_mode)[-3:] == "600"


def test_a_directory_destination_is_refused(service, payload):
    svc, root = service
    _, b64, _ = payload
    (root / "adir").mkdir()
    assert svc.terminal_put_file(str(root / "adir"), b64, overwrite=True)["error"] == "PATH_IS_DIRECTORY"


def test_no_temporary_file_is_left_behind(service, payload):
    svc, root = service
    _, b64, _ = payload
    svc.terminal_put_file(str(root / "a.bin"), b64)
    assert [p.name for p in root.iterdir() if p.name.startswith(".")] == []
