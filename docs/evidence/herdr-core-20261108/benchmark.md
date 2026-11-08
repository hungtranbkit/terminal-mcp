# herdr-core HP pilot benchmark — 2026-11-08

Host: hp-linux, 30 sequential repetitions per backend, disposable isolated
namespaces only. Herdr: 0.1.18. tmux: host binary, isolated socket
`terminal-mcp-bench2-109`. Workload per repetition: create `/bin/sh`, list,
send `echo`, read 10 lines, terminate dedicated session. Values are wall
clock milliseconds; RSS is KiB. This is not a Dell result.

| operation | tmux p50 / p95 | herdr-core p50 / p95 |
|---|---:|---:|
| create | 11.025 / 11.639 | 31.966 / 51.866 |
| list | 3.479 / 3.842 | 5.446 / 5.779 |
| send | 6.715 / 7.658 | 1023.345 / 1026.661 |
| read | 3.174 / 3.583 | 5.685 / 6.327 |
| server RSS | 4608 (median) | 6228 |

The original Herdr send loop sends immediately after creating each terminal.
Source inspection and a separate 30-run warm benchmark isolate the cost:

- for a terminal younger than 10 seconds, core waits for 800 ms without
  output (`HC_SETTLE_MS`) before accepting a paste;
- core then intentionally waits 200 ms between paste and Enter;
- after terminals were older than 10 seconds, warm send acknowledgement was
  p50 207.221 ms / p95 207.767 ms (min 203.161, max 209.118).

Thus the ~1.026 s p95 is core's cold-start dropped-paste protection, not
Python wrapper, CLI startup or socket event-loop overhead. The remaining
~207 ms is the intentional paste/submit separation. Terminal MCP does not
override either safety value. It must not present Herdr as lower control
latency than tmux. No event-to-dashboard or concurrent real-agent benchmark
was run, so those acceptance cells remain UNKNOWN.
