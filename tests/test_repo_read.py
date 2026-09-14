"""repo_read.py: the read-only repository engine behind the `repo_*` MCP
tools.

Every test here runs against a REAL git repository created in tmp_path
(real `git init`, real commits, real symlinks, a real oversized file) --
not a mocked subprocess. The containment, secret-denial and truncation
rules are the whole product here, and a mock of `git` would prove nothing
about whether `git grep`'s actual pathspec handling or `Path.resolve()`'s
actual symlink following do what this module claims they do.
"""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp import repo_read
from terminal_mcp.repo_read import RepoReadPolicy


def _git(repo, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                            check=True, env={"HOME": str(repo), "PATH": "/usr/bin:/bin",
                                             "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
                                             "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"})
    return result.stdout


@pytest.fixture
def repo(tmp_path):
    """A small but genuinely real repository: two commits, a nested
    directory, a credential file, an ordinary file with a secret pasted
    into it, an oversized file and a symlink pointing outside the repo."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    _git(root.parent, "init", "-q", "-b", "main", "project")
    (root / "README.md").write_text("# Project\n\nA sample project.\n")
    (root / "src" / "app.py").write_text(
        "def find_me():\n    return 'needle_symbol'\n\n\ndef other():\n    return 2\n")
    (root / "docs" / "guide.md").write_text("Guide\n=====\nneedle_symbol appears here too.\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@x", "-c", "user.name=T", "commit", "-q", "-m", "first commit")

    (root / "src" / "app.py").write_text(
        "def find_me():\n    return 'needle_symbol'\n\n\ndef other():\n    return 3\n")
    (root / "CHANGELOG.md").write_text("- changed other() to return 3\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@x", "-c", "user.name=T", "commit", "-q", "-m", "second commit")

    # Uncommitted change, so working-tree diff/status have something real.
    (root / "README.md").write_text("# Project\n\nA sample project, edited.\n")

    # A credential file (denied by NAME) and an ordinary file that happens
    # to contain a secret (caught only by REDACTION) -- two different
    # defences, deliberately tested apart.
    (root / ".env").write_text("OPENAI_API_KEY=sk-should-never-be-returned\n")
    (root / "config.sample.yaml").write_text(
        "host: localhost\npassword = hunter2-should-be-redacted\ntoken: ghp_" + "a" * 40 + "\n")

    # Outside the repo, plus a symlink inside it pointing there.
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("this file is outside the repo and must never be read\n")
    (root / "escape-link").symlink_to(outside)
    (root / "escape-dir").symlink_to(tmp_path)

    (root / "big.txt").write_text("".join(f"line {n:05d}\n" for n in range(1, 5001)))
    return root


@pytest.fixture
def policy(tmp_path):
    return RepoReadPolicy(allowed_roots=(str(tmp_path),))


# -- local happy path ----------------------------------------------------

def test_status_reports_branch_head_dirtiness_and_project_identity(repo, policy):
    result = repo_read.repo_status(str(repo), policy)
    assert "error" not in result
    assert result["branch"] == "main"
    assert len(result["head"]) == 40
    # There IS a real uncommitted edit plus untracked files.
    assert result["clean"] is False
    assert any("README.md" in line for line in result["status_lines"])
    # No upstream is configured, and that is NOT a failure -- it must be
    # reported as has_upstream=False with zeroed counts, never guessed.
    assert result["has_upstream"] is False
    assert (result["ahead"], result["behind"], result["diverged"]) == (0, 0, False)
    assert result["project"]["repo_root"] == str(repo)


def test_head_returns_the_current_commit(repo, policy):
    result = repo_read.repo_head(str(repo), policy)
    assert result["subject"] == "second commit"
    assert result["branch"] == "main"
    assert result["detached"] is False
    assert result["head"].startswith(result["short"])


def test_branches_lists_the_current_branch(repo, policy):
    result = repo_read.repo_branches(str(repo), policy)
    names = {branch["name"]: branch for branch in result["branches"]}
    assert "main" in names
    assert names["main"]["current"] is True
    assert names["main"]["upstream"] is None


def test_tree_lists_files_and_descends_to_the_requested_depth(repo, policy):
    result = repo_read.repo_tree(str(repo), policy, depth=1, limit=100)
    paths = {entry["path"] for entry in result["entries"]}
    assert "README.md" in paths
    assert "src" in paths
    assert "src/app.py" in paths  # depth=1 means one level BELOW the root
    assert result["truncated"] is False


def test_tree_never_descends_into_dot_git(repo, policy):
    result = repo_read.repo_tree(str(repo), policy, depth=3, limit=2000)
    assert not any(entry["path"].startswith(".git/") for entry in result["entries"])


def test_read_returns_a_line_window_with_honest_line_numbers(repo, policy):
    result = repo_read.repo_read(str(repo), policy, file="src/app.py", start_line=1, end_line=2)
    assert result["content"] == "def find_me():\n    return 'needle_symbol'"
    assert (result["start_line"], result["end_line"], result["lines_returned"]) == (1, 2, 2)


def test_read_accepts_a_full_path_instead_of_a_repo_plus_file(repo, policy):
    result = repo_read.repo_read(str(repo / "README.md"), policy)
    assert result["path"] == "README.md"
    assert "A sample project" in result["content"]


def test_search_finds_a_real_symbol_with_path_and_line(repo, policy):
    result = repo_read.repo_search(str(repo), policy, query="needle_symbol")
    hits = {(hit["path"], hit["line"]) for hit in result["results"]}
    assert ("src/app.py", 2) in hits
    assert ("docs/guide.md", 3) in hits
    assert result["truncated"] is False


def test_search_is_fixed_string_by_default(repo, policy):
    """A regex metacharacter must be matched literally unless regex=True --
    otherwise a caller searching for `find_me()` gets a surprise."""
    assert repo_read.repo_search(str(repo), policy, query="find_me()")["count"] == 1
    assert repo_read.repo_search(str(repo), policy, query="needle.symbol")["count"] == 0
    assert repo_read.repo_search(str(repo), policy, query="needle.symbol", regex=True)["count"] >= 2


def test_search_with_no_match_is_an_empty_answer_not_an_error(repo, policy):
    """`git grep` exits 1 for "no matches". Reporting that as
    GIT_COMMAND_FAILED would make every unsuccessful search look broken."""
    result = repo_read.repo_search(str(repo), policy, query="definitely_absent_xyzzy")
    assert "error" not in result
    assert result["results"] == []


def test_diff_shows_the_uncommitted_working_tree_change(repo, policy):
    result = repo_read.repo_diff(str(repo), policy)
    assert "error" not in result
    assert "README.md" in result["patch"]
    assert "A sample project, edited." in result["patch"]


def test_diff_between_two_revisions(repo, policy):
    result = repo_read.repo_diff(str(repo), policy, base="HEAD~1", head="HEAD")
    assert "CHANGELOG.md" in result["patch"]
    assert "return 3" in result["patch"]


def test_diff_with_only_one_of_base_and_head_is_refused(repo, policy):
    assert repo_read.repo_diff(str(repo), policy, base="HEAD")["error"] == repo_read.INVALID_ARGUMENT


def test_log_returns_commits_newest_first(repo, policy):
    result = repo_read.repo_log(str(repo), policy, limit=10)
    assert [commit["subject"] for commit in result["commits"]] == ["second commit", "first commit"]


def test_log_for_one_path_only_shows_commits_touching_it(repo, policy):
    result = repo_read.repo_log(str(repo), policy, file="CHANGELOG.md")
    assert [commit["subject"] for commit in result["commits"]] == ["second commit"]


def test_show_commit_returns_metadata_message_and_patch(repo, policy):
    result = repo_read.repo_show_commit(str(repo), policy, commit="HEAD")
    assert result["subject"] == "second commit"
    assert len(result["parents"]) == 1
    assert "CHANGELOG.md" in result["patch"]


def test_show_commit_rejects_an_unknown_revision(repo, policy):
    assert repo_read.repo_show_commit(str(repo), policy,
                                      commit="deadbeef1234")["error"] == repo_read.INVALID_REF


def test_remotes_without_check_auth_touches_no_network(repo, policy):
    """The default MUST be a pure local-config read: this is what keeps a
    repository readable when remote auth is unavailable or the box is
    offline."""
    result = repo_read.repo_remotes(str(repo), policy)
    assert "error" not in result
    assert result["remotes"] == []  # the fixture has no remote configured
    assert result["auth"] == {"checked": False,
                              "detail": "pass check_auth=true to probe read access (network)"}


# -- path containment ----------------------------------------------------

def test_traversal_out_of_the_repo_is_denied(repo, policy):
    for attempt in ("../outside-secret.txt", "../../etc/passwd", "src/../../outside-secret.txt"):
        result = repo_read.repo_read(str(repo), policy, file=attempt)
        assert result["error"] == repo_read.PATH_OUTSIDE_REPO, attempt


def test_absolute_path_outside_the_repo_is_denied(repo, policy, tmp_path):
    result = repo_read.repo_read(str(repo), policy, file=str(tmp_path / "outside-secret.txt"))
    assert result["error"] == repo_read.PATH_OUTSIDE_REPO


def test_symlink_escaping_the_repo_is_denied(repo, policy):
    """The symlink target is inside an ALLOWED ROOT here, so only the
    repo-containment check can catch it -- which is exactly why the check
    happens after `.resolve()` rather than on the literal path."""
    result = repo_read.repo_read(str(repo), policy, file="escape-link")
    assert result["error"] == repo_read.PATH_OUTSIDE_REPO


def test_symlinked_directory_is_listed_but_never_walked(repo, policy):
    result = repo_read.repo_tree(str(repo), policy, depth=3, limit=2000)
    entries = {entry["path"]: entry for entry in result["entries"]}
    assert entries["escape-dir"]["symlink"] is True
    # Walking into it would have surfaced tmp_path's own sibling files.
    assert not any(path.startswith("escape-dir/") for path in entries)


def test_a_repo_outside_every_allowed_root_is_refused(repo, tmp_path):
    narrow = RepoReadPolicy(allowed_roots=(str(tmp_path / "somewhere-else"),))
    assert repo_read.repo_status(str(repo), narrow)["error"] == repo_read.REPO_NOT_ALLOWED


def test_a_repo_whose_root_sits_above_the_allowed_root_is_refused(repo):
    """Allowing only `<repo>/src` must NOT make the whole repository
    readable through relative paths -- the repo ROOT gets the same
    containment check as the requested path."""
    narrow = RepoReadPolicy(allowed_roots=(str(repo / "src"),))
    result = repo_read.repo_status(str(repo / "src"), narrow)
    assert result["error"] == repo_read.REPO_NOT_ALLOWED
    assert "repository root is outside" in result["detail"]


def test_a_directory_that_is_not_a_repo_is_reported_as_such(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    policy = RepoReadPolicy(allowed_roots=(str(tmp_path),))
    assert repo_read.repo_status(str(plain), policy)["error"] == repo_read.NOT_A_GIT_REPO


def test_disabled_config_refuses_everything(repo, tmp_path):
    off = RepoReadPolicy(enabled=False, allowed_roots=(str(tmp_path),))
    for call in (repo_read.repo_status, repo_read.repo_head):
        assert call(str(repo), off)["error"] == repo_read.REPO_READ_DISABLED


# -- secrets -------------------------------------------------------------

def test_reading_a_credential_file_is_denied(repo, policy):
    result = repo_read.repo_read(str(repo), policy, file=".env")
    assert result["error"] == repo_read.SECRET_PATH_DENIED
    assert "sk-should-never-be-returned" not in str(result)


def test_a_denied_path_answers_the_same_whether_or_not_it_exists(repo, policy):
    """SECRET_PATH_DENIED comes before the existence check on purpose: a
    different answer for present vs absent would turn this into an oracle
    for "does this machine have an id_ed25519"."""
    assert repo_read.repo_read(str(repo), policy,
                               file="id_ed25519")["error"] == repo_read.SECRET_PATH_DENIED
    assert repo_read.repo_read(str(repo), policy,
                               file="deploy/.ssh/id_rsa")["error"] == repo_read.SECRET_PATH_DENIED


def test_denied_paths_are_listed_in_the_tree_but_flagged(repo, policy):
    """Hiding them would make an agent conclude the file is absent; the
    path is safe to show, only the content is not."""
    result = repo_read.repo_tree(str(repo), policy, depth=1, limit=200)
    env = next(entry for entry in result["entries"] if entry["path"] == ".env")
    assert env["denied"] is True
    assert env["reason"] == repo_read.SECRET_PATH_DENIED


def test_search_drops_hits_inside_credential_files_and_says_so(repo, policy):
    """Without this, a secret is readable one grep line at a time -- the
    exact hole a path-only rule leaves open."""
    result = repo_read.repo_search(str(repo), policy, query="OPENAI_API_KEY")
    assert all(hit["path"] != ".env" for hit in result["results"])
    assert ".env" in result["secret_paths_skipped"]
    assert "sk-should-never-be-returned" not in str(result)


def test_a_secret_inside_an_ordinary_file_is_redacted_not_refused(repo, policy):
    """The path rules cannot catch this file -- `config.sample.yaml` is a
    perfectly ordinary name -- so redaction is the only thing standing
    between the caller and the value."""
    result = repo_read.repo_read(str(repo), policy, file="config.sample.yaml")
    assert "error" not in result
    assert "host: localhost" in result["content"]      # benign content survives
    assert "hunter2-should-be-redacted" not in result["content"]
    assert "ghp_" + "a" * 40 not in result["content"]
    assert "<REDACTED>" in result["content"]
    assert result["redaction"]["redactions"] >= 2
    # The report names RULES, never matched values -- it gets logged.
    assert "hunter2-should-be-redacted" not in str(result["redaction"])


def test_a_diff_hunk_for_a_credential_file_is_excluded(repo, policy):
    _git(repo, "add", "-f", ".env")
    result = repo_read.repo_diff(str(repo), policy, staged=True)
    assert ".env" in result["secret_paths_excluded"]
    assert "sk-should-never-be-returned" not in result["patch"]
    assert repo_read.SECRET_PATH_DENIED in result["patch"]  # the omission is visible


def test_log_for_a_credential_path_is_denied(repo, policy):
    assert repo_read.repo_log(str(repo), policy,
                              file=".env")["error"] == repo_read.SECRET_PATH_DENIED


def test_a_binary_file_is_refused_rather_than_mangled(repo, policy):
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02binary\x00payload")
    assert repo_read.repo_read(str(repo), policy,
                               file="blob.bin")["error"] == repo_read.BINARY_FILE


# -- output limits -------------------------------------------------------

def test_read_enforces_the_byte_limit_and_reports_truncation(repo):
    tight = RepoReadPolicy(allowed_roots=(str(repo.parent),), max_bytes=2_048)
    result = repo_read.repo_read(str(repo), tight, file="big.txt")
    assert result["truncated"] is True
    assert len(result["content"].encode()) <= 2_048
    assert result["byte_limit"] == 2_048
    # Truncation cuts back to a whole line, so quoted line numbers stay real.
    assert result["content"].splitlines()[-1].startswith("line ")


def test_read_cannot_be_argued_past_the_configured_byte_limit(repo):
    tight = RepoReadPolicy(allowed_roots=(str(repo.parent),), max_bytes=2_048)
    result = repo_read.repo_read(str(repo), tight, file="big.txt", max_bytes=10_000_000)
    assert result["byte_limit"] == 2_048
    assert len(result["content"].encode()) <= 2_048


def test_read_enforces_the_line_limit(repo):
    tight = RepoReadPolicy(allowed_roots=(str(repo.parent),), max_lines=25)
    result = repo_read.repo_read(str(repo), tight, file="big.txt", start_line=1, end_line=4000)
    assert result["lines_returned"] == 25
    assert result["truncated"] is True


def test_search_enforces_the_result_limit(repo):
    tight = RepoReadPolicy(allowed_roots=(str(repo.parent),), max_results=2)
    result = repo_read.repo_search(str(repo), tight, query="line", max_results=500)
    assert result["limit"] == 2
    assert len(result["results"]) <= 2


def test_tree_enforces_the_entry_limit_and_reports_truncation(repo):
    tight = RepoReadPolicy(allowed_roots=(str(repo.parent),), max_tree_entries=3)
    result = repo_read.repo_tree(str(repo), tight, depth=5, limit=999)
    assert result["count"] <= 3
    assert result["truncated"] is True


def test_log_enforces_the_entry_limit(repo):
    tight = RepoReadPolicy(allowed_roots=(str(repo.parent),), max_log_entries=1)
    assert len(repo_read.repo_log(str(repo), tight, limit=50)["commits"]) == 1


def test_diff_enforces_the_byte_limit(repo):
    tight = RepoReadPolicy(allowed_roots=(str(repo.parent),), max_diff_bytes=1_024)
    _git(repo, "add", "-A")
    result = repo_read.repo_diff(str(repo), tight, staged=True)
    assert len(result["patch"].encode()) <= 1_024
    assert result["truncated"] is True


# -- argument safety -----------------------------------------------------

def test_a_ref_that_looks_like_a_flag_is_refused(repo, policy):
    for hostile in ("--upload-pack=touch /tmp/x", "--help", "--output=/etc/passwd"):
        assert repo_read.repo_show_commit(str(repo), policy,
                                          commit=hostile)["error"] == repo_read.INVALID_REF


def test_a_pathspec_that_looks_like_a_flag_is_refused(repo, policy):
    result = repo_read.repo_search(str(repo), policy, query="x", paths=["--output=/etc/passwd"])
    assert result["error"] == repo_read.INVALID_ARGUMENT


def test_a_pathspec_climbing_out_of_the_repo_is_refused(repo, policy):
    assert repo_read.repo_diff(str(repo), policy,
                               paths=["../.."])["error"] == repo_read.PATH_OUTSIDE_REPO


def test_an_unknown_parameter_is_an_error_not_silently_ignored(repo, policy):
    result = repo_read.run_operation("read", str(repo), {"fille": "README.md"}, policy)
    assert result["error"] == repo_read.INVALID_ARGUMENT
    assert result["unknown_params"] == ["fille"]


def test_run_operation_rejects_an_unknown_operation(repo, policy):
    assert repo_read.run_operation("exec", str(repo), {}, policy)["error"] == \
        repo_read.INVALID_ARGUMENT


# -- the read-only guarantee ---------------------------------------------

def test_no_write_capable_git_subcommand_is_reachable():
    """V1's central promise. Structural, not conventional: `_run_git`
    refuses anything outside the allowlist, so a future edit that tries to
    add a write path fails loudly instead of shipping."""
    forbidden = {"checkout", "switch", "reset", "clean", "commit", "push", "fetch", "pull",
                 "merge", "rebase", "stash", "apply", "am", "cherry-pick", "revert", "gc",
                 "worktree", "update-ref", "add", "rm", "mv", "tag", "init", "clone",
                 "config", "filter-branch", "reflog", "prune", "submodule", "bisect"}
    assert repo_read.READ_ONLY_GIT_SUBCOMMANDS & forbidden == set()


def test_running_a_write_subcommand_raises_rather_than_executing(repo, policy):
    with pytest.raises(ValueError, match="non-read-only"):
        repo_read._run_git(repo, "checkout", "main", policy=policy)
    with pytest.raises(ValueError, match="non-read-only"):
        repo_read._run_git(repo, "push", policy=policy)


def test_the_operation_table_exposes_only_the_ten_reads():
    """A new entry here is a new capability on every surface at once (MCP
    tool, node endpoint, local client), so the set is pinned."""
    assert set(repo_read.OPERATION_NAMES) == {
        "status", "head", "branches", "remotes", "tree", "read", "search", "diff", "log",
        "show_commit"}
    # Each entry resolves to a real function in this module, so the table
    # cannot name something that does not exist (or, worse, silently
    # resolve to a builtin).
    for name, spec in repo_read.OPERATIONS.items():
        assert spec["func"].startswith("repo_")
        assert callable(getattr(repo_read, spec["func"]))


def test_git_calls_never_prompt_for_credentials():
    """A read that blocks on a passphrase prompt would hang until the
    timeout instead of answering GIT_AUTH_REQUIRED."""
    env = repo_read._git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["SSH_ASKPASS"] == ""
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


def test_extra_secret_globs_can_only_add_denials(repo, policy):
    wider = RepoReadPolicy(allowed_roots=policy.allowed_roots,
                           extra_secret_globs=("CHANGELOG.md",))
    assert repo_read.repo_read(str(repo), wider,
                               file="CHANGELOG.md")["error"] == repo_read.SECRET_PATH_DENIED
    # Every built-in glob still applies alongside the operator's additions.
    assert repo_read.repo_read(str(repo), wider, file=".env")["error"] == \
        repo_read.SECRET_PATH_DENIED


# -- config --------------------------------------------------------------

def test_config_defaults_are_on_with_no_roots_of_their_own():
    """Read-only access is on by default (no write primitive to guard), but
    `allowed_roots` is deliberately EMPTY so the effective allowlist comes
    from the deployment's already-curated session_lifecycle roots rather
    than from a default invented here."""
    from terminal_mcp.config import RepoReadConfig

    defaults = RepoReadConfig()
    assert defaults.enabled is True
    assert defaults.allowed_roots == ()


def test_config_falls_back_to_the_session_lifecycle_allowlist():
    from terminal_mcp.config import RepoReadConfig

    policy = RepoReadConfig().to_policy(("/srv/work", "/home/dev"))
    assert policy.allowed_roots == ("/srv/work", "/home/dev")
    # An explicit list wins over the fallback.
    explicit = RepoReadConfig(allowed_roots=("/only/here",)).to_policy(("/srv/work",))
    assert explicit.allowed_roots == ("/only/here",)


def test_config_never_falls_back_to_an_unbounded_root():
    """With no roots configured anywhere, the effective allowlist is the
    server's own home directory -- never "/"."""
    from pathlib import Path

    from terminal_mcp.config import RepoReadConfig

    policy = RepoReadConfig().to_policy(())
    assert policy.allowed_roots == ()
    assert policy.resolved_roots() == [Path.home().resolve()]


@pytest.mark.parametrize("bad", [
    {"enabled": "yes"},
    {"allowed_roots": ["/"]},
    {"allowed_roots": ["relative/path"]},
    {"allowed_roots": "not-a-list"},
    {"max_bytes": 0},
    {"max_bytes": 99_000_000},
    {"max_lines": 0},
    {"max_results": 0},
    {"max_tree_depth": True},
    {"timeout_seconds": 999},
    {"extra_secret_globs": "not-a-list"},
])
def test_config_rejects_a_nonsensical_limit_rather_than_clamping_it(bad):
    """A caps section nobody can trust is worse than no caps: an
    out-of-range limit is a config ERROR, surfaced at load time, not
    silently corrected into something the operator did not ask for."""
    from terminal_mcp.config import _load_repo_read_config

    with pytest.raises(ValueError, match="repo_read"):
        _load_repo_read_config(bad)


def test_a_root_of_slash_is_refused_because_it_would_void_the_allowlist():
    from terminal_mcp.config import _load_repo_read_config

    with pytest.raises(ValueError, match="may not contain"):
        _load_repo_read_config({"allowed_roots": ["/"]})


# -- line windows are reachable regardless of the byte cap ---------------

def test_a_line_window_late_in_a_big_file_is_reachable_despite_a_small_byte_cap(repo):
    """The regression this guards: slicing the first `max_bytes` bytes and
    THEN indexing lines silently clamped a late window to whatever line the
    byte cap landed on -- the caller asked for line 4000 and got line ~200,
    with nothing in the response saying the window had moved. Every line
    number quoted afterwards would then be wrong."""
    tight = RepoReadPolicy(allowed_roots=(str(repo.parent),), max_bytes=2_048)
    result = repo_read.repo_read(str(repo), tight, file="big.txt",
                                 start_line=4000, end_line=4002)
    assert result["start_line"] == 4000
    assert result["lines_returned"] == 3
    assert result["content"].splitlines() == ["line 04000", "line 04001", "line 04002"]


def test_a_window_wider_than_the_byte_cap_returns_whole_lines_and_reports_truncation(repo):
    tight = RepoReadPolicy(allowed_roots=(str(repo.parent),), max_bytes=64)
    result = repo_read.repo_read(str(repo), tight, file="big.txt",
                                 start_line=100, end_line=900)
    assert result["start_line"] == 100
    assert result["truncated"] is True
    assert len(result["content"].encode()) <= 64
    # Whole lines only -- never a half line the caller might quote.
    assert all(line.startswith("line 0") for line in result["content"].splitlines())


def test_a_start_line_past_the_end_of_the_file_is_refused_not_clamped(repo, policy):
    """Answering a different window than was asked for is worse than
    refusing: the caller cannot tell it happened."""
    result = repo_read.repo_read(str(repo), policy, file="src/app.py", start_line=9_000)
    assert result["error"] == repo_read.INVALID_ARGUMENT
    assert "past the end" in result["detail"]


def test_a_window_still_obeys_the_line_limit(repo):
    tight = RepoReadPolicy(allowed_roots=(str(repo.parent),), max_lines=10)
    result = repo_read.repo_read(str(repo), tight, file="big.txt",
                                 start_line=1000, end_line=4000)
    assert result["lines_returned"] == 10
    assert result["start_line"] == 1000
    assert result["truncated"] is True


def test_the_scan_bound_is_separate_from_the_return_cap():
    """max_bytes bounds what is RETURNED; MAX_LINE_SCAN_BYTES bounds what is
    LOOKED THROUGH. Conflating them is what caused the bug above."""
    assert repo_read.MAX_LINE_SCAN_BYTES > RepoReadPolicy().max_bytes * 10
