"""One canonical answer to "what is sitting in the agent's composer right now".

THE FAILURE THIS EXISTS FOR
---------------------------

Captured live on 2026-09-15 from a real, running Claude Code session
(``mcp-work``), the last rows of the pane are::

    ✻ Brewed for 3m 13s · done 6:39 AM
                                  ✔ Update installed · Restart to update
    ────────────────────────────────────────────────────────────────────
    ❯ Đóng backlog item, còn lại tách rollout item riêng
    ────────────────────────────────────────────────────────────────────
      ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents

Two independent readers existed before this module -- ``core._extract_
composer_text`` ("last non-empty line, split on the first ``"> "``") and
``submit_flow.extract_composer_text`` (reverse-scan for a ``>``/``❯``/``›``
marker followed by an ASCII space). **Both returned the footer**
``"⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"`` on that pane,
because the character between the ``❯`` marker and the prompt is U+00A0, a
NON-BREAKING space, not U+0020. The marker test ``startswith("❯ ")`` fails,
the reverse scan finds no marker anywhere, and the fallback ("last non-empty
line") hands back UI furniture that lives *below* the composer box.

Everything downstream is computed from that string, so every one of these was
being decided against footer chrome rather than against the prompt:

* ``_send_enter_key_verified_locked``'s ``expected_text`` -- the echo the
  busy-window ack rule requires. The footer *changes* when Claude starts
  working (``auto mode on`` -> the busy indicator), so the echo could never be
  found and a genuinely accepted submit reported ``DELIVERY_UNKNOWN`` /
  ``SUBMIT_UNCONFIRMED``. That is the observed false negative on ``mcp-work``
  and ``terminal-mcp-main``.
* the ``SUBMIT_STALLED`` classifier, which compares the two readers' answers.
* ``submit_flow.submission_id``, whose digest is ``pane_identity`` + composer
  text -- footer text is IDENTICAL across prompts and across sessions in the
  same UI state, so independent submissions collided onto one id.
* ``plan_submit``'s ``NOTHING_TO_SUBMIT`` / ``COMPOSER_UNSTABLE`` gates.

So this module is deliberately ONE implementation that both call sites now
delegate to. Two readers that "mirror" each other is how they drifted.

WHAT IT DOES
------------

1. Normalises every Unicode space separator to U+0020 before matching, so a
   NBSP-padded marker reads exactly like an ASCII-padded one.
2. Reverse-scans for a composer marker, skipping recognised chrome (box-draw
   rules, the auto-mode/permission footer, the update notice, the working
   indicator). ``⏵`` is explicitly NOT a marker -- ``⏵⏵ auto mode on`` is the
   footer, and treating it as a composer would reintroduce the same bug.
3. Falls back to the last non-chrome, non-empty line when no marker is
   present at all -- the long-standing behaviour several callers and tests
   rely on, now merely refusing to return obvious furniture.

Pure: no tmux, no I/O, no state. Every rule here is unit-testable against a
captured pane, which is what let the live failure above be reproduced without
a real CLI.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

# Composer prompt markers, most specific first. `⏵` is deliberately absent:
# `⏵⏵ auto mode on ...` is the FOOTER, below the composer box.
MARKERS: tuple[str, ...] = ("❯", "›", "»", ">")

# Lines that are chrome, never a composer. Matched against the space-
# normalised, stripped line.
_CHROME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[─━═\-_·•\s]+$"),                    # box rules / separators
    re.compile(r"^[│┃║]\s*$"),                          # bare box sides
    re.compile(r"⏵+\s*(auto|plan|accept)", re.I),       # ⏵⏵ auto mode on ...
    re.compile(r"\bshift\+tab to cycle\b", re.I),
    re.compile(r"\besc to interrupt\b", re.I),
    re.compile(r"\bctrl\+[a-z]\b.*\b(to|for)\b", re.I),  # ctrl+o for details, ...
    re.compile(r"\bUpdate installed\b", re.I),
    re.compile(r"\bRestart to update\b", re.I),
    re.compile(r"^\s*✻|^\s*✽|^\s*✢",),                   # working/spinner glyph rows
    re.compile(r"\b\d+k tokens\b", re.I),                # "/clear to save 891k tokens"
)


def normalize_spaces(text: str) -> str:
    """Every Unicode space separator becomes U+0020.

    ``unicodedata.category(c) == "Zs"`` is the whole family (U+00A0 NBSP,
    U+2007 figure space, U+202F narrow NBSP, the U+2000..U+200A run, U+3000
    ideographic space). Matching on the category rather than a hand-listed
    set is what stops the next unlisted space character from reopening this
    bug -- the live failure was exactly one unlisted character.
    """
    return "".join(" " if unicodedata.category(ch) == "Zs" else ch for ch in text)


def is_chrome(line: str) -> bool:
    """True for a line that is UI furniture rather than content."""
    stripped = normalize_spaces(line).strip()
    if not stripped:
        return True
    return any(pattern.search(stripped) for pattern in _CHROME_PATTERNS)


@dataclass(frozen=True)
class ComposerRead:
    """What the composer holds, and how confident the reader is about it."""
    text: str
    found_marker: bool
    line_index: int | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text


def read(snapshot: Sequence[str] | None) -> ComposerRead:
    """Reverse-scan a pane tail for the composer line.

    A marker with nothing after it is a present-but-empty composer -- an
    important distinction from "no composer found", because an empty composer
    means there is genuinely nothing to submit, while a missing one means the
    reader could not tell.
    """
    lines = list(snapshot or ())
    for offset, raw in enumerate(reversed(lines)):
        normalized = normalize_spaces(raw).strip()
        if not normalized:
            continue
        index = len(lines) - 1 - offset
        for marker in MARKERS:
            if normalized == marker:
                return ComposerRead("", found_marker=True, line_index=index)
            if normalized.startswith(marker + " "):
                return ComposerRead(normalized[len(marker):].strip(),
                                    found_marker=True, line_index=index)
        if is_chrome(raw):
            continue
        # A non-chrome, non-marker line: remember it as the fallback but keep
        # looking upward -- a composer marker above it still wins.
        fallback_index, fallback_text = index, normalized
        for deeper in reversed(lines[:index]):
            deeper_normalized = normalize_spaces(deeper).strip()
            if not deeper_normalized:
                continue
            for marker in MARKERS:
                if deeper_normalized == marker:
                    return ComposerRead("", found_marker=True)
                if deeper_normalized.startswith(marker + " "):
                    return ComposerRead(deeper_normalized[len(marker):].strip(),
                                        found_marker=True)
            break
        return ComposerRead(fallback_text, found_marker=False, line_index=fallback_index)
    return ComposerRead("", found_marker=False)


def extract(snapshot: Sequence[str] | None) -> str:
    """The composer's current text, or "" -- the drop-in replacement for both
    historical readers."""
    return read(snapshot).text


def holds(snapshot: Sequence[str] | None, text: str) -> bool:
    """Does the composer still hold exactly `text`?

    Compared after space normalisation and whitespace collapsing, so a prompt
    that re-wrapped or was re-padded between two captures is still recognised
    as the same pending prompt rather than looking like a new one.
    """
    if not text:
        return False
    return _collapse(extract(snapshot)) == _collapse(text)


def _collapse(text: str) -> str:
    return " ".join(normalize_spaces(text).split())
