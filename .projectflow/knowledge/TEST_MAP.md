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

Call the runbook by OPERATION rather than composing a command:
`work_procedures(procedure_id="test")` -- also `build`, `deploy`, `smoke`,
`health`. It resolves and registers from the scripts above on first use,
reuses a green result while nothing it depends on changed, and answers in one
line. The `bash scripts/agent/...` forms above are what it runs; read one only
when a run FAILS.
