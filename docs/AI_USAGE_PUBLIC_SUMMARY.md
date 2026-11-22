# Read-only AI Usage summary for ProjectFlow

This is an **authenticated node-agent GET**, not a new public account API.

## Source and output contract

On the Dell Linux node only, the existing AI Usage Monitor serves
`http://127.0.0.1:8787/api/usage`. The node-agent route
`GET /v1/ai-usage/summary` reads that exact loopback source with the
existing bounded `ai_usage_client.fetch_usage` and a short in-memory cache.

The response keeps only `codex`, `claude`, `gemini`, `antigravity`,
their availability state, the observed-at timestamp, and numeric percent
used/remaining for actual 5-hour/weekly windows with source-provided reset
times. Unreported windows stay absent, **not zero**. A transient source
failure is a non-sensitive `source_status=unavailable` and retries sooner.

The response **never** contains account/email, plan, API key, OAuth file,
user ID, model inventory, source errors, prompts, terminal transcript,
absolute paths, costs or per-session token breakdown. This is quotas
on the Dell machine, **not** agent session tokens or a claim about another
machine. It is not a new credential collection workflow.

Existing bearer auth and throttle apply. There is no POST or mutating
action on this route. No provider API key or OAuth file is read by the
Terminal MCP route itself; the local monitor remains responsible for its
own data. The 8787 listener remains loopback-only.

## ProjectFlow public boundary

HP reads the Dell node-agent endpoint via its existing authorized node
connection; it does **not** open 8787 to the network or put bearer tokens
in GitHub Pages. HP applies a second strict allowlist and rejects malformed,
future-dated, stale (over 15 minutes), inconsistent or nonnumeric quota
values. The public page displays only source node, generic provider,
used/remaining %, validated reset time and explicit availability state.
No raw account metadata is ever exported.

## Rollout and rollback

Test the route with TestClient, then loopback canary on Dell. Run focused
node-agent and AI-usage regressions. Only then update Dell's existing
node-agent service, preserving every running tmux session, its controller
connection and its node ID. Verify authenticated GET from HP; allow the
ProjectFlow SSE cache to fill; inspect the public mobile view. If the
endpoint or node-agent health fails, roll the Dell service back to the
previous source release, leaving provider monitor and credentials untouched.

An older node without this optional endpoint simply returns 404 to the
caller and the public UI displays `unavailable` rather than guessed 0%.
