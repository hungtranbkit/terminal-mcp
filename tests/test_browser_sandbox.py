"""The managed worker must never silently launch Chromium without its sandbox."""
from contextlib import nullcontext
from types import SimpleNamespace
import sys

import pytest

from terminal_mcp import browser_worker as worker


def install_driver(monkeypatch, launch):
    driver = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    monkeypatch.setitem(sys.modules, "playwright.sync_api",
                        SimpleNamespace(sync_playwright=lambda: nullcontext(driver)))
    monkeypatch.setattr(worker, "GuardedProxy",
                        lambda _: nullcontext(SimpleNamespace(url="http://127.0.0.1:1")))


def test_worker_explicitly_requires_chromium_sandbox(monkeypatch):
    launches = []
    def launch(**kwargs):
        launches.append(kwargs)
        return SimpleNamespace(version="synthetic", close=lambda: None)
    install_driver(monkeypatch, launch)
    result = worker.run_job({"op": "probe"})
    assert result["ok"] is True
    assert len(launches) == 1
    assert launches[0].get("chromium_sandbox") is True
    assert "--no-sandbox" not in launches[0]["args"]


@pytest.mark.parametrize("flag", [
    "--no-sandbox", "--no-sandbox=true", "--disable-setuid-sandbox",
    "--disable-seccomp-filter-sandbox", "--disable-namespace-sandbox",
    "--single-process", "--no-zygote",
])
def test_sandbox_disabling_argument_refused_before_proxy_or_launch(monkeypatch, flag):
    def forbidden(*args, **kwargs):
        pytest.fail("unsafe request reached browser/proxy startup")
    install_driver(monkeypatch, forbidden)
    monkeypatch.setattr(worker, "GuardedProxy", forbidden)
    result = worker.run_job({"op": "probe", "browser_args": ["--disable-gpu"] * 20 + [flag]})
    assert result["ok"] is False
    assert result["error"] == "BROWSER_SANDBOX_REQUIRED"


def test_sandbox_launch_failure_does_not_retry_unsandboxed(monkeypatch):
    launches = []
    def launch(**kwargs):
        launches.append(kwargs)
        raise RuntimeError("Failed to move to new namespace: Operation not permitted")
    install_driver(monkeypatch, launch)
    result = worker.run_job({"op": "probe"})
    assert result["ok"] is False
    assert result["error"] == "BROWSER_LAUNCH_FAILED"
    assert len(launches) == 1
    assert launches[0].get("chromium_sandbox") is True
