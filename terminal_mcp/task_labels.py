"""Current task label per session ("TASK HIỆN TẠI" on /dashboard/live).

WHY THIS EXISTS
---------------
Direct dispatch (`create_session` -> `send`/`send_wait`) re-uses sessions for
one task after another, and nothing durable said which task a session is on
NOW -- the Live Session Monitor could only show the last raw send preview,
which after the first "y"/"continue" no longer describes the work at all.

This module owns the rule for when a session's label changes and the
deterministic summary used when no explicit title is given. Rows live in the
existing audit DB (`session_task_labels`, AuditStore migration 5), keyed by
(node_id, session). No LLM is ever involved: the prompt is summarized locally
and redacted before it is stored.

LABEL RULE (keep in sync with terminal_turn's help text)
--------------------------------------------------------
* explicit `title` (or `metadata.task_summary`) on send / send_wait / start /
  enqueue / supervise / create_session -> ALWAYS replaces the label
  (source "title").
* create_session with `initial_prompt` -> summary of the prompt
  (source "initial_prompt"); create_session without one CLEARS any label a
  previous same-named session left behind ("Chưa gắn task").
* start / enqueue (durable) and supervise -> title, else prompt summary
  (source "durable_task" / "supervised").
* send / send_wait WITHOUT a title never replace an existing label. They may
  set the FIRST label of an unlabeled session, and only when the text is
  substantial new work (`is_substantial_prompt`): not a continuation/control
  word, >= 6 words and >= 40 characters after normalization
  (source "prompt_fallback").
* a label is only written after the submission was accepted (SUBMIT_CONFIRMED
  / task accepted / session created); a blocked or failed send changes nothing.
* nothing clears a label when a session goes IDLE; the last task stays
  visible until a new one replaces it. delete_session removes the row.
"""
from __future__ import annotations

import re
from typing import Any

from .redaction import redact_text

SUMMARY_MAX_CHARS = 140

SOURCE_TITLE = "title"
SOURCE_INITIAL_PROMPT = "initial_prompt"
SOURCE_DURABLE = "durable_task"
SOURCE_SUPERVISED = "supervised"
SOURCE_FALLBACK = "prompt_fallback"
# Sources whose summary was derived from prompt text rather than a label the
# caller chose to publish. The monitor role-gates these like text previews.
PROMPT_DERIVED_SOURCES = frozenset({SOURCE_INITIAL_PROMPT, SOURCE_DURABLE,
                                    SOURCE_SUPERVISED, SOURCE_FALLBACK})

# Words that only steer a turn already in progress.
_CONTINUATION_WORDS = {
    "y", "yes", "n", "no", "ok", "okay", "k", "go", "continue", "cont", "proceed",
    "next", "done", "approve", "approved", "accept", "confirm", "retry", "again",
    "sure", "yep", "yeah", "please", "pls", "go ahead", "keep going", "carry on",
    "tiếp", "tiếp tục", "làm tiếp", "đồng ý", "ok tiếp", "có", "không", "được",
}
_CONTINUATION_PREFIX = re.compile(
    r"^(?:y|yes|ok|okay|continue|proceed|go ahead|keep going|carry on|tiếp tục|làm tiếp|"
    r"approved?|confirm(?:ed)?|retry)\b[\s,.!:;-]*", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xX]\]\s+)?|\d{1,3}[.)]\s+)+")
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|`+|~~)")
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")
_LABEL_ONLY = re.compile(r"^[\W_]+$")
_TITLE_KEYS = ("task_summary", "title")


def _is_section_label(raw: str, line: str) -> bool:
    words = len(line.split())
    return (raw.lstrip().startswith("#") and words <= 2) or (line.endswith(":") and words <= 3)


def _clean_line(line: str) -> str:
    line = _MD_LINK.sub(r"\1", line)
    line = _MD_PREFIX.sub("", line)
    line = _MD_EMPHASIS.sub("", line)
    return " ".join(line.split()).strip()


def summarize_prompt(text: Any, limit: int = SUMMARY_MAX_CHARS) -> str | None:
    """Deterministic one-line summary of a prompt, or None if it has none.

    Normalizes markdown (headings, bullets, emphasis, links, code fences),
    takes the first meaningful line (a bare section label such as "# Task"
    or "Goal:" defers to the line under it), then its first sentence when
    that sentence is >= 12 chars, redacts secrets, and caps at `limit` on a
    word boundary with "…".
    """
    if not isinstance(text, str) or not text.strip():
        return None
    in_fence = False
    candidates: list[str] = []
    for raw in redact_text(text).splitlines():
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = _clean_line(raw)
        if not line or _LABEL_ONLY.match(line):
            continue
        # A bare section label ("# Task", "Goal:") names a section rather
        # than the work; prefer the line under it when there is one.
        if _is_section_label(raw, line) and not candidates:
            candidates.append(line)
            continue
        candidates.append(line)
        break
    if not candidates:
        return None
    chosen = candidates[-1]
    first = _SENTENCE_END.split(chosen, maxsplit=1)[0].strip()
    if len(first) >= 12:
        chosen = first
    chosen = chosen.rstrip(" :;,-")
    if not chosen:
        return None
    if len(chosen) > limit:
        cut = chosen[: limit - 1]
        space = cut.rfind(" ")
        if space >= limit * 0.6:
            cut = cut[:space]
        chosen = cut.rstrip(" ,;:-") + "…"
    return chosen


def normalize_title(title: Any, limit: int = SUMMARY_MAX_CHARS) -> str | None:
    """An explicit title, cleaned the same way (one line, capped, redacted)."""
    if not isinstance(title, str):
        return None
    return summarize_prompt(title, limit)


def title_from(title: Any = None, metadata: Any = None) -> str | None:
    """Explicit title, else metadata.task_summary / metadata.title."""
    explicit = normalize_title(title)
    if explicit:
        return explicit
    if isinstance(metadata, dict):
        for key in _TITLE_KEYS:
            value = normalize_title(metadata.get(key))
            if value:
                return value
    return None


def is_continuation(text: Any) -> bool:
    """True for control/continuation input: "y", "continue", approvals,
    tiny follow-ups. Such a send must never relabel a session."""
    if not isinstance(text, str):
        return True
    compact = " ".join(text.split()).strip().casefold().rstrip(".!?")
    if not compact:
        return True
    if compact in _CONTINUATION_WORDS:
        return True
    words = compact.split()
    if len(words) <= 3 and len(compact) <= 24:
        return True
    rest = _CONTINUATION_PREFIX.sub("", compact, count=1)
    return rest != compact and len(rest.split()) < 6


def is_substantial_prompt(text: Any) -> bool:
    """Conservative "clearly new work" test for an untitled direct send."""
    if is_continuation(text):
        return False
    summary = summarize_prompt(text)
    if not summary:
        return False
    return len(summary) >= 40 and len(summary.split()) >= 6


class TaskLabeler:
    """Applies the label rule against an AuditStore-compatible store.

    Every method is best-effort: a label must never break a send, a create
    or a start, so storage errors are swallowed and reported as None.
    """

    def __init__(self, store: Any, *, local_node_id: Any = None) -> None:
        self.store = store
        self._local_node_id = local_node_id

    def _node(self, node_id: Any) -> str:
        if node_id:
            return str(node_id)
        local = self._local_node_id() if callable(self._local_node_id) else self._local_node_id
        return str(local or "local")

    @staticmethod
    def split_target(target: Any) -> tuple[str | None, str | None]:
        if not isinstance(target, str) or not target.strip():
            return None, None
        target = target.strip().removeprefix("session:")
        if "/" in target:
            node, _, name = target.partition("/")
            return (node or None), (name or None)
        return None, target

    def _write(self, node_id: Any, session: Any, summary: str | None, source: str, *,
               task_id: Any = None, request_key: Any = None) -> dict[str, Any] | None:
        if not session or not summary:
            return None
        try:
            return self.store.set_task_label(
                node_id=self._node(node_id), session=str(session), summary=summary,
                source=source, task_id=(str(task_id) if task_id else None),
                request_key=(str(request_key) if request_key else None))
        except Exception:  # noqa: BLE001 -- a label never breaks the real action
            return None

    def current(self, node_id: Any, session: Any) -> dict[str, Any] | None:
        try:
            return self.store.get_task_label(self._node(node_id), str(session))
        except Exception:  # noqa: BLE001
            return None

    def on_send(self, *, node_id: Any, session: Any, text: Any, title: Any = None,
                metadata: Any = None) -> dict[str, Any] | None:
        explicit = title_from(title, metadata)
        if explicit:
            return self._write(node_id, session, explicit, SOURCE_TITLE)
        if not is_substantial_prompt(text):
            return None
        if self.current(node_id, session) is not None:
            return None  # untitled sends never replace an existing label
        return self._write(node_id, session, summarize_prompt(text), SOURCE_FALLBACK)

    def on_task(self, *, node_id: Any, session: Any, text: Any, title: Any = None,
                metadata: Any = None, task_id: Any = None, request_key: Any = None,
                source: str = SOURCE_DURABLE) -> dict[str, Any] | None:
        explicit = title_from(title, metadata)
        return self._write(node_id, session, explicit or summarize_prompt(text),
                           SOURCE_TITLE if explicit else source,
                           task_id=task_id, request_key=request_key)

    def on_create(self, *, node_id: Any, session: Any, initial_prompt: Any = None,
                  title: Any = None) -> dict[str, Any] | None:
        explicit = title_from(title)
        summary = explicit or summarize_prompt(initial_prompt)
        if summary:
            return self._write(node_id, session, summary,
                               SOURCE_TITLE if explicit else SOURCE_INITIAL_PROMPT)
        self.clear(node_id, session)
        return None

    def clear(self, node_id: Any, session: Any) -> None:
        if not session:
            return
        try:
            self.store.delete_task_label(self._node(node_id), str(session))
        except Exception:  # noqa: BLE001
            pass
