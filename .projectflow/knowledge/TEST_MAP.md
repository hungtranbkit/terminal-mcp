# Test Map

Commands that have actually been run and passed in this project.

- **Full regression:** `bash scripts/agent/test-gate.sh` -- last full green run
  reported `3342 passed, 1 skipped, 16 deselected` in about 14 minutes.
- **Fast UI gate:** `bash scripts/agent/test-ui-fast.sh` -- Playwright browser
  assertions against the dashboard.
- **Health:** `bash scripts/agent/healthcheck.sh` -- returns e.g.
  `PASS stage=exceptions summary="active pid=..., no exceptions in 5m"`.
- **Smoke:** `bash scripts/agent/smoke.sh`.

Playwright on this host needs `--no-sandbox --no-zygote --single-process
--disable-gpu`.

One test is skipped by design: `tests/test_session_lifecycle.py` skips when
`codex` is not installed on the host.

Prefer the registered runbook over composing a command: `work_procedures`
lists them, and a green result is reused when nothing it depends on changed.
