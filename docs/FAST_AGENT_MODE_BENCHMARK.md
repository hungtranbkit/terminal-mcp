# Fast Agent Mode round-trip benchmark

## Method

Count client orchestration calls from task submission through completion. The
baseline is the TMCP-FAST-AGENT-001 audit estimate for a coding task handled by
repeated inspect/send/wait turns. The after measurement is the disposable E2E
in `tests/test_fast_agent_e2e.py`: one `CompactTerminalTools.turn(action="start")`
call, a real local tmux send, durable queue progress, and a completion marker
verified by the real queue engine. The test reads the final durable row from
its harness for assertion; that is test instrumentation, not a second client
orchestration call.

## Results

| Path | Client calls through completion | Poll calls during execution | Evidence |
| --- | ---: | ---: | --- |
| Before: inspect/send/wait loop | 10–30 (audit estimate) | 9–29 | TMCP-FAST-AGENT-001 audit |
| After: one submit, server carries task | 1 | 0 | `test_one_turn_call_advances_real_durable_task_without_client_polling` |
| After: submit plus user-requested result | 2 | 0 | One submit plus one `task_status` read |

This measures orchestration round trips, not coding time, test duration, or
model reasoning. The E2E uses a disposable fake Codex TUI, a temporary git
repository, and an isolated tmux session; it makes no model API calls and
touches no production session. The queue engine and acceptance/completion
checks are real. No latency reduction is claimed from this call-count result.
