"""Central admission, provider cooldown, and bounded transient retry policy."""
from __future__ import annotations

import email.utils
import hashlib
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from . import metrics
from .config import LLMGovernorConfig
from .queue_store import DISPATCHING, DISPATCH_UNCERTAIN, PRECHECK, READY, RUNNING, VERIFYING, QueueTask

_LOGGER = logging.getLogger(__name__)
_RATE_LIMIT_RE = re.compile(r"(?:\b429\b|too many requests|rate[ -]?limit(?:ed| exceeded)?)", re.IGNORECASE)
_RESERVED_STATUSES = (PRECHECK, READY, DISPATCHING, DISPATCH_UNCERTAIN, RUNNING, VERIFYING)
_ACTIVE_STATUSES = (DISPATCHING, DISPATCH_UNCERTAIN, RUNNING, VERIFYING)


def provider_for_task(task: QueueTask) -> str:
    """Resolve provider without reading prompt content or credentials."""
    metadata = task.metadata or {}
    explicit = str(metadata.get("provider") or metadata.get("agent_type") or "").strip().casefold()
    if explicit:
        for provider in ("openrouter", "codex", "claude"):
            if provider in explicit:
                return provider
        return explicit
    hints = f"{metadata.get('model') or ''} {task.session}".casefold()
    for provider in ("openrouter", "codex", "claude"):
        if provider in hints:
            return provider
    return "unknown"


def _queue_wait_ms(task: QueueTask) -> int:
    try:
        created = datetime.fromisoformat(task.created_at.replace("Z", "+00:00"))
        return max(0, round((datetime.now(timezone.utc) - created).total_seconds() * 1000))
    except (TypeError, ValueError):
        return 0


def retry_after_seconds(headers: Mapping[str, Any] | None, *, now: datetime | None = None) -> float | None:
    if not headers:
        return None
    value = next((v for k, v in headers.items() if str(k).casefold() == "retry-after"), None)
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - (now or datetime.now(timezone.utc))).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _status_of(value: Any) -> int | None:
    for name in ("status_code", "status", "http_status"):
        candidate = getattr(value, name, None)
        if candidate is None and isinstance(value, Mapping):
            candidate = value.get(name)
        try:
            return int(candidate) if candidate is not None else None
        except (TypeError, ValueError):
            pass
    response = getattr(value, "response", None)
    return _status_of(response) if response is not None and response is not value else None


def _headers_of(value: Any) -> Mapping[str, Any] | None:
    headers = getattr(value, "headers", None)
    if headers is None and isinstance(value, Mapping):
        headers = value.get("headers")
    response = getattr(value, "response", None)
    if headers is None and response is not None and response is not value:
        return _headers_of(response)
    return headers if isinstance(headers, Mapping) else None


def _transient(value: Any) -> bool:
    status = _status_of(value)
    if status == 429 or (status is not None and 500 <= status <= 599):
        return True
    return isinstance(value, (ConnectionError, TimeoutError))


class RequestGovernor:
    """One source of truth for task admission in this server process.

    Reservations are represented by the queue's existing PRECHECK/READY/etc.
    states, so restart recovery counts work already in flight and never opens a
    second burst merely because the process restarted.
    """

    def __init__(self, config: LLMGovernorConfig, store: Any, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 random_fn: Callable[[], float] = random.random) -> None:
        self.config = config
        self.store = store
        self.monotonic = monotonic
        self.sleep = sleep
        self.random_fn = random_fn
        self._lock = threading.RLock()
        self._cooldown_until: dict[str, float] = {}
        self._consecutive_429: dict[str, int] = {}
        self._last_429_fingerprint: dict[str, str] = {}

    def _tasks(self, statuses: tuple[str, ...]) -> list[QueueTask]:
        return self.store.tasks_with_statuses(statuses)

    def try_admit_and_claim(self, session: str, *, claimed_by: str,
                            lease_seconds: float) -> tuple[QueueTask | None, str | None]:
        """Atomically (within this process) check capacity then claim one task."""
        with self._lock:
            candidate = self.store.next_dispatchable_task(session)
            if candidate is None:
                return None, None
            provider = provider_for_task(candidate)
            now = self.monotonic()
            cooldown = self._cooldown_until.get(provider, 0.0)
            reserved = self._tasks(_RESERVED_STATUSES)
            global_used = len(reserved)
            provider_used = sum(provider_for_task(task) == provider for task in reserved)
            reason = None
            if cooldown > now:
                reason = f"provider_cooldown retry_after_ms={round((cooldown - now) * 1000)}"
            elif global_used >= self.config.global_max_concurrency:
                reason = "global_limit"
            elif provider_used >= self.config.provider_limit(provider):
                reason = "provider_limit"
            if reason:
                metrics.increment("llm.admission_queued")
                _LOGGER.info(
                    "LLM_ADMISSION queued task=%s provider=%s reason=%s active=%s/%s provider_active=%s/%s",
                    candidate.id, provider, reason, global_used, self.config.global_max_concurrency,
                    provider_used, self.config.provider_limit(provider))
                return None, reason
            claimed = self.store.claim_next_task(
                session, claimed_by=claimed_by, lease_seconds=lease_seconds)
            if claimed is not None:
                metrics.increment("llm.admission_started")
                _LOGGER.info(
                    "LLM_ADMISSION started task=%s provider=%s queue_wait_ms=%s active=%s/%s provider_active=%s/%s",
                    claimed.id, provider, _queue_wait_ms(claimed), global_used + 1, self.config.global_max_concurrency,
                    provider_used + 1, self.config.provider_limit(provider))
            return claimed, None

    def note_output(self, task: QueueTask, output: str) -> bool:
        """Open/extend provider cooldown when local agent output proves a 429."""
        if not output or not _RATE_LIMIT_RE.search(output[-8000:]):
            return False
        provider = provider_for_task(task)
        fingerprint = hashlib.sha256(output[-8000:].encode("utf-8", "replace")).hexdigest()
        with self._lock:
            if self._last_429_fingerprint.get(task.id) == fingerprint:
                return False
            self._last_429_fingerprint[task.id] = fingerprint
            count = self._consecutive_429.get(provider, 0) + 1
            self._consecutive_429[provider] = count
            delay = max(self.config.cooldown_429_seconds,
                        min(self.config.retry_max_delay_seconds,
                            self.config.retry_base_delay_seconds * (2 ** (count - 1))))
            self._cooldown_until[provider] = max(
                self._cooldown_until.get(provider, 0.0), self.monotonic() + delay)
        metrics.increment("llm.provider_429")
        _LOGGER.warning(
            "LLM_RETRY provider=%s task=%s status=429 owner=provider_cli cooldown_ms=%s",
            provider, task.id, round(delay * 1000))
        return True

    def note_success(self, task: QueueTask) -> None:
        provider = provider_for_task(task)
        with self._lock:
            self._consecutive_429.pop(provider, None)
            self._cooldown_until.pop(provider, None)
            self._last_429_fingerprint.pop(task.id, None)

    def execute_with_retry(self, provider: str, task_id: str, fn: Callable[[], Any]) -> Any:
        """Retry owner for direct provider adapters; SDK retries must be disabled.

        The current tmux CLI adapters do not call this method: their provider
        SDK is inside the CLI, so the CLI remains the sole retry owner and this
        governor only applies admission/cooldown.  Direct adapters can use this
        method without introducing a second retry layer.
        """
        last_error: Exception | None = None
        for attempt in range(1, self.config.retry_max_attempts + 1):
            try:
                result = fn()
                status = _status_of(result)
                if status is not None and (status == 429 or 500 <= status <= 599):
                    raise ProviderTransientResponse(result)
                return result
            except Exception as exc:  # classify narrowly before retrying
                original = exc.value if isinstance(exc, ProviderTransientResponse) else exc
                if not _transient(original) or attempt >= self.config.retry_max_attempts:
                    raise
                last_error = exc
                retry_after = retry_after_seconds(_headers_of(original)) if _status_of(original) == 429 else None
                if retry_after is None:
                    delay = min(self.config.retry_max_delay_seconds,
                                self.config.retry_base_delay_seconds * (2 ** (attempt - 1)))
                    if self.config.retry_jitter:
                        delay = min(self.config.retry_max_delay_seconds, delay * (0.5 + self.random_fn()))
                else:
                    # Retry-After is a provider-supplied minimum. Never jitter
                    # below it or cap it to our exponential-backoff ceiling.
                    delay = retry_after
                metrics.increment("llm.retry_attempt")
                _LOGGER.warning(
                    "LLM_RETRY task=%s provider=%s status=%s attempt=%s delay_ms=%s",
                    task_id, provider, _status_of(original), attempt, round(delay * 1000))
                self.sleep(delay)
        raise last_error or RuntimeError("retry exhausted")  # pragma: no cover

    def status(self) -> dict[str, Any]:
        reserved = self._tasks(_RESERVED_STATUSES)
        active = [task for task in reserved if task.status in _ACTIVE_STATUSES]
        queued = self._tasks(("QUEUED",))
        providers: dict[str, dict[str, Any]] = {}
        names = {provider_for_task(task) for task in reserved} | {"openrouter", "codex", "claude"}
        now = self.monotonic()
        for provider in sorted(names):
            providers[provider] = {
                "active": sum(provider_for_task(task) == provider for task in active),
                "reserved": sum(provider_for_task(task) == provider for task in reserved),
                "limit": self.config.provider_limit(provider),
                "cooldown_remaining_ms": max(0, round((self._cooldown_until.get(provider, 0.0) - now) * 1000)),
            }
        return {
            "global_active": len(active), "global_reserved": len(reserved),
            "global_limit": self.config.global_max_concurrency,
            "queued": len(queued), "queue_wait_timeout_seconds": self.config.queue_wait_timeout_seconds,
            "providers": providers,
        }


class ProviderTransientResponse(RuntimeError):
    def __init__(self, value: Any) -> None:
        super().__init__(f"transient provider response: {_status_of(value)}")
        self.value = value
