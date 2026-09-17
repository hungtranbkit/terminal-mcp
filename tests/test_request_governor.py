from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from terminal_mcp.config import LLMGovernorConfig, load_config
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import COMPLETED, DISPATCHING, READY, RUNNING, VERIFYING, QueueStore
from terminal_mcp.request_governor import RequestGovernor, retry_after_seconds


def _finish(store: QueueStore, task_id: str) -> None:
    store.transition_task(task_id, READY, event_type="READY")
    store.transition_task(task_id, DISPATCHING, event_type="DISPATCHED")
    store.transition_task(task_id, RUNNING, event_type="STARTED")
    store.transition_task(task_id, VERIFYING, event_type="VERIFYING")
    store.transition_task(task_id, COMPLETED, event_type="COMPLETED")


def test_global_concurrency_queues_then_drains_ten_tasks(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    for index in range(10):
        store.append_tasks(f"lane-{index}", [{"prompt": "p"}])
    governor = RequestGovernor(LLMGovernorConfig(global_max_concurrency=2), store)
    completed = 0
    max_reserved = 0
    while completed < 10:
        claimed = []
        for index in range(10):
            task, _reason = governor.try_admit_and_claim(
                f"lane-{index}", claimed_by="test", lease_seconds=300)
            if task:
                claimed.append(task)
        max_reserved = max(max_reserved, governor.status()["global_reserved"])
        assert len(claimed) <= 2
        for task in claimed:
            _finish(store, task.id)
            completed += 1
    assert max_reserved == 2
    assert completed == 10


def test_provider_limit_is_independent_of_global_limit(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    for index in range(6):
        store.append_tasks(f"lane-{index}", [{"prompt": "p", "metadata": {"provider": "openrouter"}}])
    config = LLMGovernorConfig(global_max_concurrency=4, openrouter_max_concurrency=2)
    governor = RequestGovernor(config, store)
    claimed = [governor.try_admit_and_claim(
        f"lane-{index}", claimed_by="test", lease_seconds=300)[0] for index in range(6)]
    assert sum(task is not None for task in claimed) == 2
    assert governor.status()["providers"]["openrouter"]["reserved"] == 2


def test_twenty_task_load_smoke_global_three_provider_two(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    for index in range(20):
        provider = "openrouter" if index % 2 == 0 else "codex"
        store.append_tasks(
            f"{provider}-lane-{index}",
            [{"prompt": "p", "metadata": {"provider": provider}}])
    governor = RequestGovernor(
        LLMGovernorConfig(global_max_concurrency=3, openrouter_max_concurrency=2,
                          codex_max_concurrency=2), store)
    completed = 0
    observed_max_global = 0
    observed_max_provider = {"openrouter": 0, "codex": 0}
    while completed < 20:
        claimed = []
        for index in range(20):
            provider = "openrouter" if index % 2 == 0 else "codex"
            task, _reason = governor.try_admit_and_claim(
                f"{provider}-lane-{index}", claimed_by="smoke", lease_seconds=300)
            if task:
                claimed.append(task)
        status = governor.status()
        observed_max_global = max(observed_max_global, status["global_reserved"])
        for provider in observed_max_provider:
            observed_max_provider[provider] = max(
                observed_max_provider[provider], status["providers"][provider]["reserved"])
        assert claimed
        for task in claimed:
            _finish(store, task.id)
            completed += 1
    assert completed == 20
    assert observed_max_global == 3
    assert observed_max_provider["openrouter"] <= 2
    assert observed_max_provider["codex"] <= 2


class _Response:
    def __init__(self, status_code: int, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def test_429_retries_exactly_until_success_without_tight_loop(tmp_path):
    sleeps = []
    calls = 0
    governor = RequestGovernor(
        LLMGovernorConfig(retry_base_delay_seconds=2, retry_jitter=False),
        QueueStore(tmp_path / "queue.db"), sleep=sleeps.append)

    def call():
        nonlocal calls
        calls += 1
        return _Response(429 if calls < 3 else 200)

    assert governor.execute_with_retry("openrouter", "t1", call).status_code == 200
    assert calls == 3
    assert sleeps == [2, 4]


def test_retry_after_is_respected_without_real_sleep(tmp_path):
    sleeps = []
    calls = 0
    governor = RequestGovernor(
        LLMGovernorConfig(retry_jitter=True), QueueStore(tmp_path / "queue.db"),
        sleep=sleeps.append, random_fn=lambda: 0.0)

    def call():
        nonlocal calls
        calls += 1
        return _Response(429, {"Retry-After": "7"}) if calls == 1 else _Response(200)

    governor.execute_with_retry("codex", "t2", call)
    assert sleeps == [7]


def test_retry_after_http_date_is_supported():
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    header = format_datetime(now + timedelta(seconds=9), usegmt=True)
    assert retry_after_seconds({"Retry-After": header}, now=now) == 9


def test_deterministic_4xx_is_not_retried(tmp_path):
    calls = 0
    governor = RequestGovernor(LLMGovernorConfig(), QueueStore(tmp_path / "queue.db"), sleep=lambda _: None)

    def call():
        nonlocal calls
        calls += 1
        return _Response(400)

    assert governor.execute_with_retry("claude", "t3", call).status_code == 400
    assert calls == 1


def test_same_429_output_only_extends_cooldown_once(tmp_path):
    now = [100.0]
    store = QueueStore(tmp_path / "queue.db")
    (task_id,) = store.append_tasks("claude-lane", [{"prompt": "p"}])
    task = store.get_task(task_id)
    governor = RequestGovernor(LLMGovernorConfig(cooldown_429_seconds=30), store,
                               monotonic=lambda: now[0])
    assert governor.note_output(task, "429 Too Many Requests") is True
    first = governor.status()["providers"]["claude"]["cooldown_remaining_ms"]
    now[0] += 5
    assert governor.note_output(task, "429 Too Many Requests") is False
    second = governor.status()["providers"]["claude"]["cooldown_remaining_ms"]
    assert first == 30_000
    assert second == 25_000


def test_concurrent_request_key_creates_one_task(tmp_path):
    service = QueueService(QueueStore(tmp_path / "queue.db"))

    def submit(_):
        return service.enqueue("lane", "same", request_key="same-key")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert len({result["task_id"] for result in results}) == 1
    assert sum(not result["deduplicated"] for result in results) == 1


def test_governor_environment_overrides(monkeypatch):
    monkeypatch.setenv("LLM_GLOBAL_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("LLM_OPENROUTER_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("LLM_RETRY_MAX_ATTEMPTS", "5")
    config = load_config("config.example.yaml").llm_governor
    assert config.global_max_concurrency == 3
    assert config.openrouter_max_concurrency == 1
    assert config.retry_max_attempts == 5
