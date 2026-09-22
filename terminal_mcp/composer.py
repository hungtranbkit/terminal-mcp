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
    if any("\x1b" in line for line in lines):
        evidence = read_composer(lines)
        if evidence.state != COMPOSER_UNKNOWN:
            return ComposerRead(evidence.draft_text, found_marker=True, line_index=evidence.marker_row)
        lines = [strip_ansi(line) for line in lines]
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


# ANSI evidence supplements the Unicode/plain reader above.
# Composer state vocabulary. Deliberately three-valued: "I can see there
# is nothing to submit" and "I cannot tell" are different facts and must
# never collapse into one bucket -- that collapse is the bug above.
COMPOSER_DRAFT = "DRAFT"      # real, non-dim pending text: there IS something to submit
COMPOSER_EMPTY = "EMPTY"      # composer empty (blank, or showing only a dim ghost suggestion)
COMPOSER_UNKNOWN = "UNKNOWN"  # no composer marker found, or no SGR information available
COMPOSER_STATES = (COMPOSER_DRAFT, COMPOSER_EMPTY, COMPOSER_UNKNOWN)

# Every CSI sequence (tmux -e only ever emits SGR runs, but matching the
# full CSI grammar keeps this correct if a caller ever hands it a rawer
# stream) plus OSC and two-byte escapes.
_CSI = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANY_ESC = re.compile(r"\x1b.")

# Composer prompt markers actually observed in these CLIs: Claude Code
# uses U+276F, Codex uses U+203A or a plain ">". The separator Claude
# emits after its marker is U+00A0 (non-breaking space), not U+0020 --
# matched explicitly rather than relying on str.strip() semantics.
_MARKER_CHARS = MARKERS
_SEPARATORS = (" ", " ", "\t", "")

# A box-drawing rule row (the composer frame Claude/Codex draw above and
# below the input area). Used to bound a multi-row draft without ever
# swallowing conversation content below the box.
_RULE_ROW = re.compile(r"^[─-╿\-_=]{8,}$")


def strip_ansi(text: str) -> str:
    """Plain text with every escape sequence removed -- the exact string a
    non-`-e` `tmux capture-pane -p` would have produced for this row."""
    return _ANY_ESC.sub("", _CSI.sub("", _OSC.sub("", text)))


def _faint_map(raw_line: str) -> tuple[str, list[bool], bool]:
    """Return (plain_text, faint_flags_per_char, saw_any_sgr).

    Walks the row once, tracking only SGR 2 (faint/dim). SGR 0 and SGR 22
    both clear it -- 22 is "normal intensity" (the exact reset Claude Code
    pairs with its own 2, confirmed on the wire) and 0 is a full reset.
    Any other SGR parameter is irrelevant here and deliberately ignored:
    colour changes are not evidence of anything for this question.

    `saw_any_sgr` is what separates "this row is genuinely not dim" from
    "this capture carries no attribute information at all" (a plain, non-
    `-e` capture, or the Windows backend's already-resolved snapshot).
    Without it, a stripped capture would look exactly like a real draft
    and produce a confident, wrong DRAFT verdict.
    """
    plain: list[str] = []
    faint: list[bool] = []
    is_faint = False
    saw_sgr = False
    index = 0
    length = len(raw_line)
    while index < length:
        match = _CSI.match(raw_line, index) or _OSC.match(raw_line, index)
        if match:
            token = match.group(0)
            if token.endswith("m"):
                saw_sgr = True
                body = token[2:-1]
                # Extended colours consume their following parameters. RGB
                # channels / palette index 2 are not the SGR 2 dim flag.
                parts = body.split(";") if body else ["0"]
                offset = 0
                while offset < len(parts):
                    part = parts[offset]
                    head = part.split(":")[0].strip()
                    if head in {"38", "48", "58"} and ":" not in part:
                        mode = parts[offset + 1] if offset + 1 < len(parts) else ""
                        offset += 5 if mode == "2" else 3 if mode == "5" else 1
                        continue
                    if head in ("", "0"):
                        is_faint = False
                    elif head == "2":
                        is_faint = True
                    elif head == "22":
                        is_faint = False
                    offset += 1
            index = match.end()
            continue
        if raw_line[index] == "\x1b":
            index += 2  # a two-byte escape -- not an SGR, carries no attribute
            continue
        plain.append(raw_line[index])
        faint.append(is_faint)
        index += 1
    return "".join(plain), faint, saw_sgr


@dataclass(frozen=True)
class ComposerSnapshot:
    """What the composer looked like in ONE capture.

    `state` is the authoritative field. `draft_text` is non-empty ONLY for
    COMPOSER_DRAFT -- a ghost suggestion's text is deliberately NOT
    exposed as draft text anywhere, because handing it to a caller is
    exactly how it ends up being treated as a pending prompt again.
    `ghost_text` keeps it separately, for diagnostics/receipts only.
    """
    state: str
    draft_text: str = ""
    ghost_text: str = ""
    marker_row: int | None = None
    has_attributes: bool = False

    @property
    def has_draft(self) -> bool:
        return self.state == COMPOSER_DRAFT

    @property
    def is_empty(self) -> bool:
        return self.state == COMPOSER_EMPTY

    def same_draft_as(self, other: "ComposerSnapshot") -> bool:
        return (self.state == COMPOSER_DRAFT and other.state == COMPOSER_DRAFT
                and normalize(self.draft_text) == normalize(other.draft_text))


def normalize(text: str) -> str:
    """Whitespace-collapsed form -- a composer word-wraps a long draft
    across rows with its own padding, so only the collapsed form is
    comparable between two captures taken at different pane widths."""
    return " ".join(text.split())


def _split_marker(plain_row: str) -> tuple[int, str] | None:
    """(index_after_marker+separator, content) for a composer prompt row,
    or None if this row is not one. The marker must be at the very start
    of the row (ignoring leading spaces) -- a ">" appearing mid-line is
    ordinary conversation text (a quote, a shell redirect), never a
    composer prompt."""
    plain_row = normalize_spaces(plain_row)
    lead = len(plain_row) - len(plain_row.lstrip(" \t"))
    rest = plain_row[lead:]
    for marker in _MARKER_CHARS:
        if not rest.startswith(marker):
            continue
        after = rest[len(marker):]
        for separator in _SEPARATORS:
            if separator and after.startswith(separator):
                return lead + len(marker) + len(separator), after[len(separator):]
        # Marker immediately followed by content/end-of-row.
        return lead + len(marker), after
    return None


def read_composer(ansi_lines: list[str], *,
                  attributes_available: bool | None = None) -> ComposerSnapshot:
    """Classify the composer from an ANSI-preserving pane capture
    (`capture_lines(..., ansi=True)`).

    Scans upward from the bottom for the LAST composer marker row -- a
    submitted prompt can leave an identical-looking `> prompt` row behind
    in scrollback, and only the bottom-most one can be the live composer.
    A draft that word-wraps continues on the rows below the marker until
    the composer box's own rule row, so those rows are folded in.

    `attributes_available` states whether the CAPTURE ITSELF can carry SGR
    attributes -- a property of the backend, not of what happens to be on
    screen right now. It must be passed explicitly by any caller that has
    a backend to ask (core.TerminalService does), because the obvious
    auto-detection ("did I see any SGR in this capture?") is wrong in the
    exact case that matters: the moment a real draft replaces the dim
    ghost, the composer row -- and often the whole visible pane -- carries
    no SGR at all, so an attribute-capable capture of a genuine draft is
    indistinguishable from an attribute-blind capture. Auto-detection is
    kept as the default (None) purely so direct callers handing this
    module bare fixture rows keep their existing behaviour.

      True  -- content with no dim run really is a real draft.
      False -- content is undecidable: always COMPOSER_UNKNOWN.
      None  -- infer from whether the capture contains any SGR at all.

    Never raises: an unreadable/absent composer is COMPOSER_UNKNOWN, which
    every caller must treat as "cannot tell", not as "nothing to submit".
    """
    if not ansi_lines:
        return ComposerSnapshot(COMPOSER_UNKNOWN)

    parsed = [_faint_map(line) for line in ansi_lines]
    any_attributes = (any(saw for _, _, saw in parsed) if attributes_available is None
                      else attributes_available)

    for row in range(len(ansi_lines) - 1, -1, -1):
        plain, faint, _ = parsed[row]
        split = _split_marker(plain)
        if split is None:
            continue
        start, content = split

        # Is this marker row the LIVE composer, or just a submitted
        # prompt's echo left behind in scrollback? Both render as `> text`.
        # Two shapes are accepted, and nothing else:
        #   1. Inside a composer box -- a rule row directly above it. This
        #      is the real Claude/Codex shape, and it is what lets a
        #      wrapped draft be folded in safely (the box bounds it).
        #   2. A bare prompt line with NOTHING non-blank below it -- the
        #      simple `> ` input line shape, where being last IS the proof
        #      that it is live.
        # A marker row with real content below it and no box around it is
        # scrollback: reporting it as a pending draft would veto a
        # submission that actually succeeded (caught by this project's own
        # claude_composer fixture, whose post-submit pane is exactly that
        # shape). Undecidable -> COMPOSER_UNKNOWN, never a guess.
        in_box = row > 0 and bool(_RULE_ROW.match(parsed[row - 1][0].strip()))
        if not in_box and any(parsed[below][0].strip() for below in range(row + 1, len(ansi_lines))):
            return ComposerSnapshot(COMPOSER_UNKNOWN, marker_row=row, has_attributes=any_attributes)

        chars = list(content)
        flags = faint[start:start + len(chars)]
        flags += [False] * (len(chars) - len(flags))

        # Fold in wrapped continuation rows -- ONLY inside a box, where the
        # rule row below bounds them. Outside a box there is no reliable
        # end marker, and folding would swallow ordinary output (a response
        # line, a footer) into the "draft" -- which is exactly what made an
        # accepted submission look like a still-pending one.
        if in_box:
            for follow in range(row + 1, len(ansi_lines)):
                next_plain, next_faint, _ = parsed[follow]
                if _RULE_ROW.match(next_plain.strip()) or _split_marker(next_plain) is not None:
                    break
                if not next_plain.strip():
                    break
                chars += [" "] + list(next_plain)
                flags += [True] + next_faint[:len(next_plain)] + [False] * max(
                    0, len(next_plain) - len(next_faint))

        # `text` keeps the real word spacing (it is the draft a caller may
        # later compare against); the dim verdict is computed over printable
        # NON-BLANK characters only -- the blanks between words carry no
        # intensity of their own and must not dilute the check.
        text = normalize("".join(chars))
        visible = [flag for char, flag in zip(chars, flags) if char.strip()]
        if not text or not visible:
            # An empty composer with no ghost at all -- still definitively
            # "nothing to submit", and that verdict needs no attributes.
            return ComposerSnapshot(COMPOSER_EMPTY, marker_row=row, has_attributes=any_attributes)
        if not any_attributes:
            # Content is present but this capture carries no attribute
            # information, so dim-vs-real is genuinely undecidable here
            # (Windows/ConPTY, or a caller that passed a stripped
            # capture). Report UNKNOWN and let the caller stay honest.
            return ComposerSnapshot(COMPOSER_UNKNOWN, marker_row=row, has_attributes=False)
        if all(visible):
            # Every visible character is dim -> a ghost suggestion, not a
            # draft. The composer is empty; there is nothing to submit.
            return ComposerSnapshot(COMPOSER_EMPTY, ghost_text=normalize(text),
                                    marker_row=row, has_attributes=True)
        return ComposerSnapshot(COMPOSER_DRAFT, draft_text=normalize(text),
                                marker_row=row, has_attributes=True)

    return ComposerSnapshot(COMPOSER_UNKNOWN, has_attributes=any_attributes)


def draft_contains(snapshot: ComposerSnapshot, text: str, *, prefix_chars: int = 80) -> bool:
    """True when `snapshot` holds a REAL draft whose text carries a bounded
    normalized prefix of `text`. A bounded prefix (never the whole string)
    because a long prompt wraps past the captured viewport -- the same
    reasoning adapters._sent_text_echoed already documents. An empty/ghost
    composer is always False here: a ghost that happens to quote the
    prompt back must never count as "our draft is still pending"."""
    if snapshot.state != COMPOSER_DRAFT:
        return False
    wanted = normalize(text)[:prefix_chars]
    return bool(wanted) and wanted in snapshot.draft_text
