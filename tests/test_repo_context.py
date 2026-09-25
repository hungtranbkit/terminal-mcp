from terminal_mcp.context_pack import RepoContextCache


def test_repo_context_cache_detects_stack_commands_and_invalidates_on_metadata_change(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"vitest run","lint":"eslint .","build":"vite build"},'
        '"dependencies":{"react":"18"}}')
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (tmp_path / "vite.config.ts").write_text("export default { server: { port: 4173 } }\n")
    cache = RepoContextCache()

    first = cache.get(tmp_path)
    assert first["package_manager"] == "pnpm"
    assert first["framework"] == "react"
    assert first["commands"] == {"test": "vitest run", "lint": "eslint .", "build": "vite build"}
    assert first["service_port"] == 4173
    assert cache.get(tmp_path) == first

    (tmp_path / "package.json").write_text('{"scripts":{"test":"npm test"}}')
    refreshed = cache.get(tmp_path)
    assert refreshed["commands"]["test"] == "npm test"
    assert refreshed["fingerprint"] != first["fingerprint"]


def test_repo_context_cache_reports_unknown_stack_without_guessing(tmp_path):
    result = RepoContextCache().get(tmp_path)
    assert result["package_manager"] is None
    assert result["framework"] is None
    assert result["service_port"] is None
