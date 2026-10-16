"""Route durable task identities without dispatching or polling an agent."""
def task_status(task_id, *, direct, queue, headless=None):
    if isinstance(task_id, str) and task_id.startswith('dt_'):
        return direct.get(task_id, refresh=False) if direct else {'error': 'DIRECT_TASK_STORE_UNAVAILABLE'}
    if isinstance(task_id, str) and task_id.startswith('hd_'):
        return headless.status(task_id) if headless else {'error': 'HEADLESS_UNAVAILABLE'}
    return queue.task_status(task_id)
