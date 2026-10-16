from types import SimpleNamespace
from terminal_mcp.task_lookup import task_status


def test_direct_lookup_is_read_only_and_not_sent_to_queue():
    calls = []
    direct = SimpleNamespace(get=lambda key, **kw: calls.append((key, kw)) or {'task': {'task_id': key}})
    queue = SimpleNamespace(task_status=lambda _: (_ for _ in ()).throw(AssertionError('wrong store')))
    assert task_status('dt_abc', direct=direct, queue=queue)['task']['task_id'] == 'dt_abc'
    assert calls == [('dt_abc', {'refresh': False})]


def test_missing_direct_store_does_not_report_queue_task_not_found():
    assert task_status('dt_abc', direct=None, queue=None)['error'] == 'DIRECT_TASK_STORE_UNAVAILABLE'
