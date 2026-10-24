"""Bounded read-only fleet work. Timed-out readers never replay mutations.

Keep a slot until the actual worker finishes, even after its caller times out.
Repeated reads join that worker; saturation rejects immediately without an
executor backlog. Scopes prevent different controllers sharing node results.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time

from . import metrics


class ReadFanout:
    def __init__(self, limit=16, per_node=2):
        self.limit = limit
        self.per_node = per_node
        self._lock = threading.Lock()
        self._pending = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=limit, thread_name_prefix='fleet-read')

    def snapshot(self):
        with self._lock:
            active = sum(not f.done() for f in self._pending.values())
        return {'active': active, 'limit': self.limit, 'per_node_limit': self.per_node,
                'queued': 0, 'retry_after_seconds': 1}

    def submit(self, scope, label, node, call):
        key = (scope, label, node)
        with self._lock:
            self._pending = {k: f for k, f in self._pending.items() if not f.done()}
            if key in self._pending:
                metrics.increment('controller.read_coalesced')
                return self._pending[key]
            node_active = sum(k[0] == scope and k[2] == node for k in self._pending)
            if len(self._pending) >= self.limit or node_active >= self.per_node:
                metrics.increment('controller.read_rejected')
                raise TimeoutError('controller read capacity busy; retry after 1s with jitter')
            future = self._executor.submit(call)
            self._pending[key] = future
            return future

    def run(self, work, *, budget_seconds, label, scope=None):
        deadline = time.monotonic() + max(0., budget_seconds)
        results, pending = {}, {}
        for node, call in work:
            if time.monotonic() >= deadline:
                results[node] = (False, TimeoutError('fleet read budget exhausted'))
                continue
            try:
                pending[self.submit(scope, label, node, call)] = node
            except TimeoutError as exc:
                results[node] = (False, exc)
        if pending:
            done, _ = concurrent.futures.wait(pending, timeout=max(0., deadline - time.monotonic()))
            for future, node in pending.items():
                if future not in done:
                    metrics.increment('controller.read_timeout')
                    results[node] = (False, TimeoutError(f'node did not answer within {budget_seconds}s fleet {label} budget'))
                else:
                    try:
                        results[node] = (True, future.result())
                    except Exception as exc:
                        results[node] = (False, exc)
        return results


READ_FANOUT = ReadFanout()
