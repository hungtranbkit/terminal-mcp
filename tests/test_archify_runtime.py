from __future__ import annotations

import io
import os
import subprocess
import time

import pytest

from terminal_mcp.archify_runtime import ArchifyRuntime, ArchifyRuntimeError


def _runtime_dir(tmp_path):
    root = tmp_path / "runtime"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "archify.mjs").write_text("// fixture\n", encoding="utf-8")
    return root


def test_missing_shared_runtime_is_reported_without_output(tmp_path):
    runtime = ArchifyRuntime(tmp_path / "missing", node_bin="node")

    assert runtime.status()["state"] == "runtime_missing"
    with pytest.raises(ArchifyRuntimeError, match="ARCHIFY_UNAVAILABLE"):
        runtime.render("architecture", tmp_path / "in.json", tmp_path / "out.html",
                       repo_root=tmp_path)


def test_old_node_version_is_a_clear_dependency_state(tmp_path):
    root = _runtime_dir(tmp_path)

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="v16.20.0\n", stderr="")

    status = ArchifyRuntime(root, node_bin="node", runner=runner).status()

    assert status["state"] == "node_too_old"
    assert "18" in status["message"]


def test_render_passes_paths_as_discrete_arguments_and_writes_real_output(tmp_path):
    root = _runtime_dir(tmp_path)
    seen = []

    def runner(argv, **kwargs):
        seen.append((list(argv), kwargs))
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="v20.1.0\n", stderr="")
        if argv[-1] == "doctor":
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
        output = tmp_path / "out file.html"
        output.write_text("<!doctype html><title>real</title>", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="rendered\n", stderr="")

    source = tmp_path / "input file.json"
    source.write_text("{}", encoding="utf-8")
    output = tmp_path / "out file.html"
    result = ArchifyRuntime(root, node_bin="node", runner=runner).render(
        "architecture", source, output, repo_root=tmp_path / "repo with spaces")

    render_argv, render_kwargs = seen[-1]
    assert render_argv[2:4] == ["render", "architecture"]
    assert render_argv[4:6] == [str(source), str(output)]
    assert render_argv[-2:] == ["--repo-root", str(tmp_path / "repo with spaces")]
    assert render_kwargs["shell"] is False
    assert result["output_bytes"] > 0


def test_non_architecture_render_never_gets_repo_root_argument(tmp_path):
    root = _runtime_dir(tmp_path)
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="v20.1.0", stderr="")
        if argv[-1] == "doctor":
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
        (tmp_path / "out.html").write_text("<html></html>", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    source = tmp_path / "in.json"
    source.write_text("{}", encoding="utf-8")
    ArchifyRuntime(root, node_bin="node", runner=runner).render(
        "workflow", source, tmp_path / "out.html", repo_root=tmp_path)

    assert "--repo-root" not in calls[-1]


def test_timeout_becomes_stable_generation_timeout(tmp_path):
    root = _runtime_dir(tmp_path)

    def runner(argv, **kwargs):
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="v20.1.0", stderr="")
        if argv[-1] == "doctor":
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    source = tmp_path / "in.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ArchifyRuntimeError, match="GENERATION_TIMEOUT"):
        ArchifyRuntime(root, node_bin="node", runner=runner, timeout=1).render(
            "sequence", source, tmp_path / "out.html")


def test_default_runner_drains_and_bounds_subprocess_output(tmp_path):
    root = _runtime_dir(tmp_path)
    seen = {}

    class Process:
        returncode = 0
        stdout = io.BytesIO(b"x" * 10_000)
        stderr = io.BytesIO(b"y" * 10_000)

        def wait(self, timeout):
            seen["timeout"] = timeout

        def kill(self):
            seen["killed"] = True

    def popen(argv, **kwargs):
        seen.update(kwargs)
        return Process()

    result = ArchifyRuntime(
        root, node_bin="node", max_output_bytes=1024, popen_factory=popen,
    )._run(["node", "fixture"], timeout=3)

    assert seen["stdout"] is subprocess.PIPE
    assert seen["stderr"] is subprocess.PIPE
    assert seen["shell"] is False
    assert len(result.stdout.encode()) <= 1040
    assert len(result.stderr.encode()) <= 1040


@pytest.mark.skipif(os.name != "posix", reason="process-group timeout behavior is POSIX-specific")
def test_descendant_inheriting_output_pipe_cannot_extend_timeout(tmp_path):
    root = _runtime_dir(tmp_path)
    started = time.monotonic()

    ArchifyRuntime(root, node_bin="node")._run(
        ["/bin/sh", "-c", "sleep 2 &"], timeout=0.2,
    )

    assert time.monotonic() - started < 1.0
