# OpenAI Secure MCP Tunnel deployment

This project uses OpenAI Secure MCP Tunnel for private ChatGPT developer-mode
access. It deliberately does not publish `terminal-mcp.mesflow.net` or add a
Cloudflare ingress route.

Prerequisites supplied from OpenAI Platform tunnel settings:

- a `tunnel_id` associated with the target ChatGPT workspace;
- a runtime API key whose principal has **Tunnels Read + Use**;
- ChatGPT Developer Mode access for the target workspace/account.

Keep the runtime key outside this repository. Once those prerequisites exist,
initialize the profile interactively from this directory:

```bash
export CONTROL_PLANE_API_KEY="<runtime API key>"
tunnel-client init \
  --sample sample_mcp_remote_no_auth \
  --profile terminal-mcp \
  --tunnel-id "<tunnel_id>" \
  --mcp-server-url http://127.0.0.1:8766/mcp
tunnel-client doctor --profile terminal-mcp --explain
tunnel-client run --profile terminal-mcp
```

Do not put `CONTROL_PLANE_API_KEY` in Git, this README, `config.yaml`, or the
Terminal MCP service. Create long-lived supervision only after `doctor` and a
real ChatGPT tool call pass.

The backend remains loopback-only, and `config.yaml` remains the sole source of
truth for `terminal_input` and the tmux whitelist.
