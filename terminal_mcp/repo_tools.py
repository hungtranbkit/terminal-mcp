"""The read-only `repo_*` MCP tools, registered onto any MCP surface.

Lifted out of mcp_app.py's build_mcp because there are now TWO surfaces
that must expose exactly these ten tools with exactly these schemas: the
full controller surface (build_mcp, 290 tools, loopback only) and the
public read-only observer surface (observer_app.py) that a hosted Claude
client reaches over the internet. Two hand-maintained copies of a
*security-relevant* tool signature is how the copies drift, so there is
one definition and both surfaces call it.

`actor` is what lands in repo_service's audit row for every call. It is a
callable on the observer surface, because there the answer is per-request
("who authenticated this token?") rather than a constant.

Why these exist: an external agent (ChatGPT over this MCP surface)
previously could not read Git or source at all -- it had to ask a
Claude session to read a file and paste the content back, which is
slow, lossy and puts a second agent's summary between the reader and
the code. These ten tools let it read directly.

V1 is READ-ONLY, enforced structurally rather than by convention:
repo_read.READ_ONLY_GIT_SUBCOMMANDS excludes every mutating git
subcommand, no tool here takes a git subcommand / shell string /
argv list, and there is deliberately no repo_write / repo_checkout /
repo_commit counterpart. Nothing below can modify a repository.

Locating the repo: pass `path` for a repo on this host, `session`
for "wherever the session I am watching is working", `project` for a
project_identity id, or `node` + `path` to be explicit. Every
response carries `node_id` and `located_by` so the caller can always
see which machine answered.

Errors are codes, not prose: REPO_READ_DISABLED, REPO_NOT_ALLOWED,
PATH_OUTSIDE_REPO, SECRET_PATH_DENIED, PATH_NOT_FOUND, NOT_A_GIT_REPO,
BINARY_FILE, INVALID_REF, INVALID_ARGUMENT, GIT_AUTH_REQUIRED,
GIT_COMMAND_FAILED, GIT_UNAVAILABLE, NODE_UNREACHABLE,
NODE_LACKS_REPO_READ, AMBIGUOUS_REPO, REPO_NOT_LOCATED.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def register_repo_tools(server: Any, repo: Any, *,
                        actor: str | Callable[[], str] = "mcp") -> None:
    """Register the ten repo_* tools on `server`, reading through `repo`."""

    def _actor() -> str:
        return actor() if callable(actor) else actor

    @server.tool()
    def repo_status(path: str = "", session: str = "", project: str = "",
                    node: str = "") -> dict:
        """Branch, HEAD, dirty state, ahead/behind and project identity for
        one repository -- the "where am I and is it clean" read.

        Locate the repo with exactly one of: `path` (a repo root or any
        path inside one), `session` (the repo that session is working in,
        resolved on the node that session actually runs on), `project` (a
        project_identity project_id or name), or `node`+`path`.

        `status_lines` are raw `git status --porcelain` lines. `diverged`
        is true only when the branch is BOTH ahead and behind -- merely
        ahead (unpushed work) or merely behind (a fast-forward away) is
        routine mid-task state, not a divergence."""
        return repo.status(path=path or None, session=session or None,
                           project=project or None, node=node or None, actor=_actor())

    @server.tool()
    def repo_head(path: str = "", session: str = "", project: str = "",
                  node: str = "") -> dict:
        """HEAD's commit id, short id, author, timestamp, subject and
        branch -- the cheapest "which commit is checked out" read, for when
        full status is more than you need."""
        return repo.head(path=path or None, session=session or None,
                         project=project or None, node=node or None, actor=_actor())

    @server.tool()
    def repo_branches(path: str = "", session: str = "", project: str = "",
                      node: str = "", limit: int = 100) -> dict:
        """Local branches, most recently committed first, each with its tip
        commit, upstream and whether it is the current one."""
        return repo.branches(limit=limit, path=path or None, session=session or None,
                             project=project or None, node=node or None, actor=_actor())

    @server.tool()
    def repo_remotes(path: str = "", session: str = "", project: str = "",
                     node: str = "", check_auth: bool = False) -> dict:
        """Configured git remotes, with any embedded credential stripped
        from every URL.

        `check_auth=False` (the default) is pure local config and touches
        no network -- so this call works, and a repository stays fully
        readable, even when remote authentication is unavailable.
        `check_auth=True` additionally probes READ access with `ls-remote`
        (read-only, bounded, every credential prompt disabled); a failed
        probe reports GIT_AUTH_REQUIRED inside `auth` rather than failing
        the call, because the remotes themselves were read fine."""
        return repo.remotes(check_auth=check_auth, path=path or None, session=session or None,
                            project=project or None, node=node or None, actor=_actor())

    @server.tool()
    def repo_tree(path: str = "", session: str = "", project: str = "", node: str = "",
                  subpath: str = "", depth: int = 2, limit: int = 200) -> dict:
        """List files and directories, breadth-first, bounded by `depth`
        and `limit` (both capped by server config; `truncated` says when a
        cap was hit).

        Includes untracked files -- a file a session just created is
        exactly what you will want to ask about. Dependency/build caches
        (node_modules, __pycache__, .venv, dist, ...) are not descended
        into. A path denied by the secret rules is still LISTED, marked
        `"denied": true`, so you can tell "refused" from "absent"; its
        content is never read."""
        return repo.tree(subpath=subpath or None, depth=depth, limit=limit,
                         path=path or None, session=session or None,
                         project=project or None, node=node or None, actor=_actor())

    @server.tool()
    def repo_read(path: str = "", session: str = "", project: str = "", node: str = "",
                  file: str = "", start_line: int = 0, end_line: int = 0,
                  max_bytes: int = 0) -> dict:
        """Read one text file's content.

        `file` is relative to the repo root. Give `start_line`/`end_line`
        for a window (1-based, inclusive), or `max_bytes` for a byte cap;
        with neither, you get the start of the file up to the server's line
        limit. `truncated` is always set honestly when a cap was hit, and
        `start_line`/`end_line`/`lines_returned` describe exactly what came
        back, so quoted line numbers are trustworthy.

        Content is redacted before it is returned (`redaction` reports how
        many rules hit, by rule name only). A credential file is refused
        outright with SECRET_PATH_DENIED, and a binary file with
        BINARY_FILE rather than being returned as mojibake."""
        return repo.read(file=file or None, start_line=start_line or None,
                         end_line=end_line or None, max_bytes=max_bytes or None,
                         path=path or None, session=session or None,
                         project=project or None, node=node or None, actor=_actor())

    @server.tool()
    def repo_search(query: str, path: str = "", session: str = "", project: str = "",
                    node: str = "", paths: list[str] | None = None, max_results: int = 100,
                    regex: bool = False, ignore_case: bool = False,
                    include_untracked: bool = True) -> dict:
        """Search file contents for `query` and return path + line number +
        the matching line.

        FIXED-STRING by default, which is what "find this symbol" wants;
        pass `regex=True` for an extended-regex search. `paths` narrows to
        git pathspecs (e.g. ["terminal_mcp", "docs/*.md"]). Binary files are
        skipped; dependency/build caches are excluded.

        A hit inside a credential file is dropped rather than returned --
        `secret_paths_skipped` names those files so the omission is visible
        instead of silent. Matched lines are redacted like any other
        content."""
        return repo.search(query=query, paths=paths, max_results=max_results, regex=regex,
                           ignore_case=ignore_case, include_untracked=include_untracked,
                           path=path or None, session=session or None,
                           project=project or None, node=node or None, actor=_actor())

    @server.tool()
    def repo_diff(path: str = "", session: str = "", project: str = "", node: str = "",
                  base: str = "", head: str = "", staged: bool = False,
                  paths: list[str] | None = None, stat_only: bool = False,
                  max_bytes: int = 0) -> dict:
        """A unified diff.

        With no `base`/`head` this is the UNCOMMITTED working-tree change
        (`staged=True` for the index instead) -- which is what "what is
        this session doing right now" actually means. Give BOTH `base` and
        `head` to diff two revisions; giving only one is INVALID_ARGUMENT
        rather than a guess. `stat_only=True` returns just the file/line
        summary.

        Hunks touching a credential path are excluded from the patch and
        named in `secret_paths_excluded` -- a diff is the obvious way a
        path-only rule would otherwise leak a secret."""
        return repo.diff(base=base or None, head=head or None, staged=staged, paths=paths,
                         stat_only=stat_only, max_bytes=max_bytes or None,
                         path=path or None, session=session or None,
                         project=project or None, node=node or None, actor=_actor())

    @server.tool()
    def repo_log(path: str = "", session: str = "", project: str = "", node: str = "",
                 limit: int = 20, file: str = "", base: str = "", head: str = "") -> dict:
        """Commit history, newest first: commit id, short id, author,
        timestamp and subject.

        `file` restricts the history to one path. Give BOTH `base` and
        `head` for the `base..head` range. Subjects and author names are
        redacted like any other content -- a commit message is a real place
        secrets get pasted."""
        return repo.log(limit=limit, file=file or None, base=base or None, head=head or None,
                        path=path or None, session=session or None,
                        project=project or None, node=node or None, actor=_actor())

    @server.tool()
    def repo_show_commit(commit: str, path: str = "", session: str = "", project: str = "",
                         node: str = "", file: str = "", stat_only: bool = False,
                         max_bytes: int = 0) -> dict:
        """One commit in full: metadata, parents, message, and its patch
        (or just the `--stat` summary with `stat_only=True`).

        `commit` is any revision git understands (a sha, `HEAD`, `HEAD~3`,
        a tag); it is validated before use and an unknown one answers
        INVALID_REF. `file` narrows the patch to one path. Credential paths
        are excluded from the patch, same as repo_diff."""
        return repo.show_commit(commit=commit, file=file or None, stat_only=stat_only,
                                max_bytes=max_bytes or None,
                                path=path or None, session=session or None,
                                project=project or None, node=node or None, actor=_actor())
