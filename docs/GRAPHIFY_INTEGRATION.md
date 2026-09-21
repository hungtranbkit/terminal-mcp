# Graphify integration

Terminal MCP uses Graphify as an optional repository-graph layer inside the
existing Project Knowledge / Context Pack path. It does **not** replace the
existing token-efficiency mechanisms.

## Why it is additive

The existing order remains:

1. Work spec / Bug spec narrows the task.
2. Project Knowledge Map identifies the relevant module.
3. Module Context Pack loads only the named modules.
4. Similar Bug / Work Reuse checks prior evidence.
5. Runbook Registry reuses known test/build/deploy procedures.
6. Graphify adds a bounded dependency/call-graph answer when a local graph is
   available.
7. Exact search and source reads widen only when the evidence still requires it.

Soft Token Budget, File/Search Budget, session knowledge, context-window
telemetry, and verification discipline remain unchanged.

## Runtime behavior

Task start is query-only. It never installs Graphify, never builds a graph,
and never performs network work. The adapter only runs when both the
`graphify` executable and `graphify-out/graph.json` already exist.

Default limits:

- Graph query budget: 900
- Graph text admitted to one module pack: 1200 characters
- Existing Context Pack / Task Knowledge caps still apply after that

Any Graphify error fails open: the old Knowledge Map / Context Pack path keeps
working.

## Install and bootstrap

The official PyPI package is `graphifyy`; the executable is `graphify`.

```bash
# isolated CLI install
uv tool install graphifyy

# initial local, code-only graph (no LLM/API key)
graphify extract . --code-only

# optional: keep code graph fresh across commits/checkouts
graphify hook install

# after pulling/merging teammate changes
graphify update .
```

Terminal MCP also exposes an optional packaging extra:

```bash
pip install -e '.[graphify]'
```

## Configuration

- `TERMINAL_MCP_GRAPHIFY=auto` (default): use Graphify when ready.
- `TERMINAL_MCP_GRAPHIFY=off`: disable it without removing files.
- `TERMINAL_MCP_GRAPHIFY_BIN=/path/to/graphify`: executable override.
- `TERMINAL_MCP_GRAPHIFY_GRAPH=/path/to/graph.json`: graph override.

Generated Graphify output should be excluded from Claude prompt-cache inputs;
see `.claudeignore`. This does not prevent tracking the graph in git if a
project chooses to share it.
