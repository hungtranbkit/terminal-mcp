# Windows one-file installer acceptance — 2026-10-31

## Executed

- Installer source: 07a9992a8959fc943b5e6277e04aa0ba1cbbb848 (PR #101).
- Windows CI run 41170476866: success. Artifact downloaded and manifest verified.
- EXE SHA-256: 8d8e14bf9b642cee11f10c060bb93dc4bcd28b9d7c5481233bd8f77b514cc00f.
- Installed copy and download: C:\\Users\\admin\\Downloads\\TerminalMCP-Setup.exe. Development build, NOT Authenticode signed.
- Live controller release: /home/kimex/.cache/terminal-mcp/release-windows-07bdffb, merged source 07bdffbb6af57058fac1f870fd465a35ffbbf662. Preserves previous production patches and includes main heartbeat fix #104 plus pairing routes.
- Service switched with KillMode=process and automatic rollback on failed readiness/route checks. Rollout result deployed=true; /health/ready=200; unauthenticated POST join/current=403 (previously 404). Existing config retained at /home/kimex/hp-rollout/controller-config.yaml.
- Rollout Python tests: 433 passed, 4 skipped (PowerShell not installed on HP). Go helper suite passed. Native Windows CI passed.
- Windows SSH login as admin with existing public key and StrictHostKeyChecking=yes succeeded. No password auth or UAC policy changed.
- EXE helper install executed from elevated remote admin session: exit 0, binary copied, protocol registered, TerminalMCPBootstrap service Running/Automatic.
- Repair on c75f315 reproduced exit 2, Access denied replacing the running helper binary. Fixed in 07a9992 using SCM stop/wait before copy, with bounded failure. Native Repair on fixed CI EXE: exit 0, service Running.
- Machine identity preserved after Repair (boolean assertion true).
- Loopback /v1/challenge: configured dashboard origin HTTP 200; unrelated origin HTTP 403.
- Windows node /v1/sessions: no token HTTP 401; existing valid node token HTTP 200. Token never printed.
- Existing Node Agent scheduled task uses MSFT_TaskBootTrigger; sshd and Tailscale are Automatic. This is configuration evidence, NOT a performed reboot acceptance.
- Public /enroll/windows-setup.ps1 downloads successfully (HTTP 200, 86511 bytes).
- Latest installer published in controller artifact store as 0.3.0-dev.20261031.2, signed=false. Temporary Tailnet file-transfer services stopped.
- Windows Codex execution through the Terminal MCP-created Windows shell returned exactly TMCP-WIN-CODEX-OK and CODEX_EXIT=0. A prior SSH attempt waited on additional stdin; piping the prompt and closing stdin resolved the test. Separate agent_type=codex session creation also returned a real process; automated interactive prompt submission in that session was not accepted as proof.
- Reproducible smoke: pipe a prompt into codex.exe exec --skip-git-repo-check -C C:\\Users\\admin\\Documents\\Codex -o <result-file> -; do not leave non-TTY stdin open.

## Owner action required

Authenticated browser verification of the Nodes URL reached Cloudflare Access sign-in. No owner session was available. Do not disable Access, mint substitute identity, expose dashboard on bootstrap, or fabricate approval.

On Windows, open:
https://terminal-dashboard.mesflow.net/dashboard/nodes?join=current&source=windows-setup

Sign in as the owner and approve the existing current-machine flow once. Helper is already installed. This owner action must precede the full new-enrollment acceptance.

## Not yet accepted

- Full double-click/UAC/browser-owner-pairing/new enrollment sequence.
- New pairing token issuance/redemption and node registration through that sequence.
- Sign-out/reboot and execution before interactive login.
- Uninstall on a disposable node (do not remove the existing shared production node).
- Production signing.

PR #101 remains draft until these acceptance conditions are evidenced.

## Continue from

HP shell: hp-shell-win-setup-audit-20261031.
Windows shell: win-shell-setup-acceptance-20261031.
M910 SSH shell: m910-shell-win-rollout-rescue-20261031.
Avoid using the main dirty HP worktree. PR worktree: /home/kimex/workspace/terminal-mcp-issue100-windows-exe.
Deployment merge worktree: /home/kimex/workspace/terminal-mcp-windows-rollout-20261031.
Rollback: remove only ~/.config/systemd/user/terminal-mcp-http.service.d/zzzzzzzzzzzzzz-windows-setup-07bdffb.conf, daemon-reload and restart terminal-mcp-http.service; prior release/drop-ins remain intact.
