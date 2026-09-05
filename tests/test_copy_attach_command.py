""""Copy attach command" dashboard action -- generates `tmux attach -t
<session>`, shell-quoting the session name safely, copies it to the
clipboard, and is shown only for a tmux-backed session (never Windows/
non-tmux).

The escaping test below actually EXECUTES the shipped JS (extracted
verbatim from DASHBOARD_HTML via regex, never reimplemented/duplicated in
Python) through a real Node.js subprocess, then round-trips the result
through Python's own POSIX shell tokenizer (shlex) -- the strongest
available proof that the generated command, if pasted into a real shell,
parses back to exactly the original session name (and never anything
else, e.g. a second shell token an injected `;`/`` ` `` could produce).
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess

import pytest

from terminal_mcp.dashboard import DASHBOARD_HTML, SESSIONS_ADMIN_HTML

NODE = shutil.which("node") or shutil.which("nodejs")


def _extract_function(html: str, name: str) -> str:
    # Balanced-brace scan rather than a regex spanning to the "next }" --
    # a regex anchored on a following "\n    }\n" silently over-matches
    # (all the way to some unrelated later function's closing brace) for
    # a single-line function body like tmuxAttachCommand's own
    # `function tmuxAttachCommand(session) { return ...; }`, which has no
    # such newline-preceded closing brace of its own.
    marker = f"function {name}("
    start = html.index(marker)
    brace_start = html.index("{", start)
    depth = 0
    for pos in range(brace_start, len(html)):
        if html[pos] == "{":
            depth += 1
        elif html[pos] == "}":
            depth -= 1
            if depth == 0:
                return html[start:pos + 1]
    raise AssertionError(f"{name}'s closing brace not found")


@pytest.mark.skipif(NODE is None, reason="node not available in this environment")
@pytest.mark.parametrize("html_name,html", [("DASHBOARD_HTML", DASHBOARD_HTML), ("SESSIONS_ADMIN_HTML", SESSIONS_ADMIN_HTML)],
                        ids=["DASHBOARD_HTML", "SESSIONS_ADMIN_HTML"])
@pytest.mark.parametrize("session_name", [
    "claude-main",                 # the common case: no quoting needed at all
    "codex-main.2",
    "weird name",                  # a space -- would never pass valid_session_name today, quoted anyway
    "foo'bar",                     # an embedded single quote -- the actual escaping edge case
    "foo';rm -rf ~;'",             # adversarial: must never become a second shell command
    "$(whoami)",                   # adversarial: must never be substituted by the shell
    "a`b`c",
])
def test_generated_attach_command_round_trips_through_a_real_shell_tokenizer(html_name, html, session_name):
    quote_fn = _extract_function(html, "shellQuote")
    attach_fn = _extract_function(html, "tmuxAttachCommand")
    script = f"""
{quote_fn}
{attach_fn}
process.stdout.write(JSON.stringify(tmuxAttachCommand(process.argv[1])));
"""
    result = subprocess.run([NODE, "-e", script, session_name], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    command = json.loads(result.stdout)
    assert command.startswith("tmux attach -t ")
    # The whole point: shlex (a real POSIX shell tokenizer) must parse
    # this back into EXACTLY ["tmux", "attach", "-t", session_name] --
    # never more tokens, never a substring, never something a shell would
    # treat as a second command or a substitution.
    tokens = shlex.split(command)
    assert tokens == ["tmux", "attach", "-t", session_name]


@pytest.mark.parametrize("html_name,html", [("DASHBOARD_HTML", DASHBOARD_HTML), ("SESSIONS_ADMIN_HTML", SESSIONS_ADMIN_HTML)],
                        ids=["DASHBOARD_HTML", "SESSIONS_ADMIN_HTML"])
def test_copy_attach_command_never_auto_executes(html_name, html):
    # Only ever passed to copyText (clipboard) -- must never appear next
    # to anything that would actually run a shell command (exec/spawn/
    # fetch-and-run). This is a narrow, literal safety net, not a general
    # audit -- it only guards the one behavior explicitly required: this
    # button copies text, it never runs anything.
    assert "tmuxAttachCommand(row.name)" in html
    assert "copyText(tmuxAttachCommand(row.name))" in html
    assert "exec(tmuxAttachCommand" not in html
    assert "spawn(tmuxAttachCommand" not in html


def test_copy_attach_command_hidden_for_non_tmux_backend_in_tab_ui():
    # DASHBOARD_HTML (main tab UI): the term-bar menu item is hidden
    # (never merely disabled-but-visible) whenever the selected session's
    # backend isn't tmux -- e.g. a Windows node's session.
    assert "const isTmuxBacked = Boolean(row) && (row.session_backend || 'tmux') === 'tmux';" in DASHBOARD_HTML
    assert "termCopyAttachBtnEl.hidden = !isTmuxBacked;" in DASHBOARD_HTML


def test_copy_attach_command_hidden_for_non_tmux_backend_in_sessions_admin():
    # SESSIONS_ADMIN_HTML (the bulk-actions admin page): the button is
    # never even created for a non-tmux-backend row (not just hidden via
    # CSS), so it can never be clicked for a Windows session by mistake.
    assert "if ((row.session_backend || 'tmux') === 'tmux') {" in SESSIONS_ADMIN_HTML
    assert "'⧉ Attach cmd'" in SESSIONS_ADMIN_HTML


def test_copy_attach_command_shows_the_word_copied_on_success():
    # Task's own explicit ask: a visible "Copied" confirmation, never a
    # silent no-op. Rendered as brief inline button feedback (this
    # project's existing convention for "Copy output" -- a genuine
    # floating toast/popup is deliberately never used elsewhere on this
    # page either, see DASHBOARD_HTML's own LIVE-badge comment), not a
    # new toast component.
    assert "'✓ Copied'" in DASHBOARD_HTML
    assert "'✓ Copied'" in SESSIONS_ADMIN_HTML
