<!-- Source of truth: hungtranbkit/cloudflare-tunnel-publisher docs/terminal-mcp-runbook.md (fe0384c) -->
# Runbook: publish a project URL via tunnel-publisher (for Terminal MCP / agents)

Use this instead of creating a new Cloudflare tunnel per app. Works on a node where
`tunnelctl doctor` passes (currently: **dell-linux** only).

## CLI (inside a node shell)

```bash
tunnelctl doctor
tunnelctl publish --hostname <sub>.mesflow.net --origin http://127.0.0.1:<port>           # dry run
tunnelctl publish --hostname <sub>.mesflow.net --origin http://127.0.0.1:<port> --apply   # publish
tunnelctl --json status <sub>.mesflow.net --check
tunnelctl unpublish <sub>.mesflow.net --apply
```

## Loopback API (node-local)

```bash
set -a; . ~/.config/tunnel-publisher/tunnel-publisher.env; set +a
curl -s -H "Authorization: Bearer $TP_API_TOKEN" -X POST http://127.0.0.1:17380/v1/routes \
  -d '{"hostname":"<sub>.mesflow.net","origin":"http://127.0.0.1:<port>","apply":true}'
```

## Interpreting results

| Exit / HTTP | Meaning | Action |
|---|---|---|
| 0 / 200 | done (or no change) | report the URL; `status --check` shows public probe |
| 2 / 409 | refused: hostname has foreign DNS / other config / not owned | choose another hostname; never modify the other owner |
| 3 / 422 | invalid hostname or origin (apex, wildcard, denied port/process) | fix input |
| 1 / 500 | origin unhealthy, Cloudflare error, or apply failed and rolled back | read `errors` + `details.rollback` |

The publisher restarts only its own connector (`cloudflared-tunnel-publisher.service`) on change.
Origins must be loopback (or explicit `TP_TRUSTED_ORIGINS`); the app must already answer HTTP < 500.
