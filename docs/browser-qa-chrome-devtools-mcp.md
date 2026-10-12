# Browser QA: Chrome Harness + Playwright + Chrome DevTools MCP

Updated 2026-10-09. This is an additive Codex QA integration, **not** a replacement for the existing Terminal MCP Browser Gateway or Chrome Harness.

## Roles

| Layer | Use |
| --- | --- |
| Chrome Harness / Browser Gateway | Reproduce real user journeys (navigation, click, forms) in an authorized browser session. |
| Playwright | Deterministic assertions, smoke/regression and clean browser contexts. |
| Chrome DevTools MCP | Inspect Chrome tab DOM, console, network, runtime JavaScript, screenshots and performance. |

QA loop: **reproduce -> assert -> diagnose -> fix -> retest -> screenshot -> report evidence**. Never mark DONE solely because code compiled or an MCP registration exists.

## Dell Codex registration (verified)

1. Check Chrome is listening on loopback only with: ss -lntp '( sport = :9222 )'. Require a valid response from: curl -fsS --max-time 3 http://127.0.0.1:9222/json/version . An open port without DevTools JSON is NOT valid.
2. Back up the existing ~/.codex/config.toml before modification.
3. Register the server just once, or inspect the existing entry via: codex mcp get chrome-devtools .

~~~bash
codex mcp add chrome-devtools -- npx -y chrome-devtools-mcp@1.10.1 \
  --browser-url=http://127.0.0.1:9222 \
  --no-usage-statistics --no-performance-crux
~~~

4. Reopen Codex so it loads this server, then actually invoke Chrome DevTools MCP tools.
5. Run: python3 scripts/chrome_devtools_mcp_smoke.py . This script tests real MCP initialization, tool enumeration, new page, DOM, JS runtime, console, network, screenshot and cleanup on example.com. It does not inspect unrelated tabs. Optional overrides: CHROME_DEVTOOLS_MCP_URL and CHROME_DEVTOOLS_MCP_PACKAGE .

At integration time Dell was Chrome 151, Node 26, Codex 0.162, MCP 1.10.1 and a valid loopback CDP endpoint. MCP exposed 30 tools. The page tools require a numeric pageId. For Codex, ALWAYS call list_pages after new_page and use the fresh ID; reusing the new_page ID caused No page found, while relisting resolved it in a real Codex CLI test.

## Regression and completion gate

- Run Browser Gateway/Playwright independently (browser_status followed by browser_verify against a safe authorized URL); record HTTP status, page/console/network errors.
- Harness reproduces actual UI journeys; DevTools MCP diagnoses actual DOM, JavaScript and failed API calls when a scenario fails. Use the correct specific pageId.
- Fix cause, rerun failing journey, run Playwright regression and inspect screenshot. PASS needs evidence from the real UI, not just reading source.
- Report machine, URL, expected and actual behavior, steps, logs/screenshots (redacted), and separate the statuses: CONFIGURED, QA VERIFIED, MERGED, DEPLOYED.

## Security / troubleshooting

- Never expose CDP publicly or bind to 0.0.0.0: CDP gives full browser control. Never read or log credentials, tokens, cookies or unrelated tabs.
- A DevTools MCP attached to the live Chrome profile can access all its tabs. Prefer dedicated QA profiles. Only inspect authenticated tabs after authorization; do not restart a real profile or bypass the Chrome remote-debugging approval prompt.
- Auto-connect is available on Chrome 144+ with explicit Chrome remote-debugging permission, but can prompt for human approval. Dell was tested with an already-valid loopback CDP endpoint instead.
- If /json/version fails, do not assume port 9222 is working: investigate the actual Chrome launch and use an isolated QA profile if needed.
- Chrome Harness and Playwright continue to work independently if DevTools MCP is unavailable.

Official guides:
https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md
https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/configuration.md
