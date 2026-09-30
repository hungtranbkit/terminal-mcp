# Paperclip local operations

TMPC-002 installs Paperclip as a **local-only orchestration pilot** on Dell Linux.
Paperclip does not replace or proxy Terminal MCP. ChatGPT can continue using
Terminal MCP directly even when Paperclip is stopped.

## Safety boundary

- Bind Paperclip to `127.0.0.1`; do not expose port 3100 through Cloudflare or LAN.
- Do not put provider/API credentials in this repository.
- Keep `TERMINAL_MCP_ENABLE_QUEUE` unset/disabled.
- Do not disable Terminal MCP direct `inspect`, `send`, session, node, or browser tools.
- One Paperclip server process per instance.

## Bootstrap

```bash
cd ~/workspace/terminal-mcp
scripts/paperclip-bootstrap.sh
```

The bootstrap pins the tested Paperclip release, validates Node >=24.11, refuses
a non-loopback host, installs the pinned CLI into  when missing, and performs
non-interactive onboarding and installs the local systemd user service.

## Service

The bootstrap installs the service. To manage it afterwards:

```bash
export PAPERCLIP_HOST=127.0.0.1
export PAPERCLIP_PORT=3100
export PAPERCLIP_OPEN_ON_LISTEN=false
paperclipai service install
paperclipai service start
paperclipai service status
```

Stop/restart:

```bash
paperclipai service stop
paperclipai service start
paperclipai service restart
```

Logs and diagnostics:

```bash
paperclipai service logs
paperclipai doctor
scripts/paperclip-health.sh
```

## Adapter prerequisites

The pilot expects the host's existing authenticated CLIs to remain available:

```bash
command -v codex && codex --version
test -s ~/.codex/auth.json
command -v claude && claude --version
```

Claude may use a local subscription login/credentials. Do not copy credentials
into the Terminal MCP repo.

## Direct Terminal MCP regression check

While Paperclip is running, and again after it is stopped:

1. `terminal_turn(action=inspect, target=<known session>)`
2. create a disposable shell session
3. `send_wait` a harmless command
4. delete/clean the disposable session

Paperclip availability must not affect any of these operations.

## Rollback

Paperclip managed install keeps prior payloads.

```bash
paperclipai service stop
paperclipai update --rollback
```

To remove only the service/runtime while preserving instance data:

```bash
paperclipai service uninstall
paperclipai uninstall
```

Do not remove `~/.paperclip/instances/` unless instance data is intentionally
being destroyed.
