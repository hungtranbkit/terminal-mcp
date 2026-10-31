# Windows setup route audit — 2026-10-31

PR #101 baseline: 22ae0b8. Main fetched: 702550f (includes #104).
Live controller working directory: /home/kimex/.cache/terminal-mcp/release-delete-confirm-20261026; checkout HEAD: 15a906c. This identifies checkout, not an independently attested in-memory build SHA.

Observed from HP:
- bootstrap /dashboard/nodes: HTTP 404.
- localhost:8766 /dashboard/nodes: HTTP 403 without authentication.
- dashboard hostname /dashboard/nodes: HTTP 302 (authentication redirect).
- cloudflared-terminal-mcp-dashboard config routes dashboard hostname /dashboard/* to controller; bootstrap hostname has narrow machine routes and a fallback http_status:404.

Root cause: generic installer opened its machine-facing DefaultController + /dashboard/nodes. This violates the deployed separation between owner dashboard and public enrollment origin. Do not expose dashboard routes on bootstrap to hide this mismatch.

Fix: embed separate public DefaultDashboard; validate HTTPS origin without userinfo/path/query/fragment; open owner approval there; seed helper DashboardOrigins with the configured origin when installing for the compiled controller. Preserve controller endpoint, UAC, one-time pairing, existing identity and authenticated approval. Stamp both origins in CI and local build.

Verification:
- Helper go test ./... passed after change.
- Windows amd64 cross-compilation succeeded (development check binary, not signed).
- New Windows Codex session created via Terminal MCP: win-codex-setup-smoke-20261031, process codex.EXE pid 11156. Initial prompt delivery confirmed; final reply not yet verified. This uses a pre-existing node and is NOT EXE installation acceptance.
- Python bootstrap/helper artifact tests: 115 passed in 14.16s using the existing main worktree test environment with PYTHONPATH set to this checkout. This checkout's own venv lacks pytest.
- join/current route string occurrences: deployed checkout 0, main 0, PR checkout 2 (JS call and route). Deployment is still required.

Still blocked / unproven:
- Production controller must serve PR #101 join/current and helper routes with existing owner auth. No production restart/deploy performed.
- Rebuild downloadable installer for this commit and verify exact artifact hash.
- Fresh EXE install with UAC + owner pairing, repair/upgrade/uninstall, pinned-host-key SSH authentication, pre-login reboot and post-install Codex execution remain unverified.
- Windows shell commands returned PENDING with no captured output; do not interpret as successful execution.
- Keep PR draft and do not publish a production release.

No secrets were embedded, logged, or copied into URLs. Bootstrap ingress security policy is unchanged.
