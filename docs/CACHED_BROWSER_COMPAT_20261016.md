# Cached browser argument compatibility

The MCP entrypoint accepts top-level `steps`, `viewport`, `screenshot`, and `allow_mutations` from the cached ChatGPT catalog and translates the supported read-only subset to the canonical BrowserGateway contract. `assert_text` and `assert_title` require a nonempty text value; `assert_status` requires an integer HTTP status. Other steps are rejected without dispatch rather than silently omitted.

A viewport requires integer width and height; the gateway retains its own caps and URL policy. Canonical/cached conflicts fail closed. The default screenshot policy sent by old wrappers is ignored on non-verification actions, preserving normal session control. Explicit mutation requests do not grant browser write authority.

This does not change delete confirmation, authentication, the sidecar cache identity or existing canonical args. It does not imply Chrome/Figma/browser mutation support.

Tests: 143 focused passed on HP; full suite timed out and is not green. No deployment. Roll back by reverting this narrow change before activation. For rollout, retain the running release, run current-SHA checks, and verify a known false assertion produces a non-PASS result with at least one executed check from the real connector.
