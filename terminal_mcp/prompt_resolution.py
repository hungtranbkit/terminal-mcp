"""Allowlisted resolver for routine, non-granting CLI startup prompts.

The resolver returns keyboard navigation only for stable menus with a
predeclared harmless choice. It never derives input from arbitrary pane text.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .lifecycle import _authorized_startup_step


@dataclass(frozen=True)
class SafePromptChoice:
    prompt_id: str
    choice_id: str
    key: str
    fingerprint: str


_CHOICES = {
    "CODEX_UPDATE_MENU": ("codex.optional_update.v1", "skip_optional_update"),
    "CODEX_STARTUP_MENU": ("codex.daemon_fallback.v1", "run_without_daemon_once"),
    "CODEX_HOOK_REVIEW_MENU": ("codex.hook_review.v1", "continue_without_trusting"),
    "CLAUDE_OPTIONAL_NOTICE_MENU": ("claude.optional_notice.v1", "dismiss_without_saving"),
}


def prompt_fingerprint(lines: list[str]) -> str:
    visible = "\n".join(" ".join(line.split()) for line in lines[-12:] if line.strip())
    return hashlib.sha256(visible.encode("utf-8", errors="replace")).hexdigest()


def classify_safe_prompt(lines: list[str], agent_type: str) -> SafePromptChoice | None:
    step = _authorized_startup_step(lines, agent_type)
    if step is None:
        return None
    reason, key = step
    choice = _CHOICES.get(reason)
    if choice is None:
        return None
    return SafePromptChoice(choice[0], choice[1], key, prompt_fingerprint(lines))


def resolve_choice_key(lines: list[str], agent_type: str, prompt_id: str,
                       choice_id: str) -> str | None:
    """Return the next safe navigation key only when prompt and choice match."""
    candidate = classify_safe_prompt(lines, agent_type)
    if candidate is None or (candidate.prompt_id, candidate.choice_id) != (prompt_id, choice_id):
        return None
    return candidate.key
