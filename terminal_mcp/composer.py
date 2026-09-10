"""Deterministic, ANSI-aware reading of an agent CLI's prompt composer.

ROOT CAUSE THIS MODULE EXISTS FOR (found live, 2026-09-10, on the
hp-linux node against real Claude Code 2.1.267 sessions hp1/hp2):

Claude Code renders a *dim ghost suggestion* inside an EMPTY composer --
a greyed-out proposed reply (e.g. "yes, publish the report" right after
Claude itself asked "Want me to publish the report ... ?"). On the wire
that row looks like::

    ESC[39m ❯ \xa0 ESC[2m yes, publish the report ESC[0m
                        ^^^^^ SGR 2 = faint/dim

A real, genuinely-pending draft the user (or this project) typed looks
like::

    ESC[39m ❯ \xa0 REAL_TYPED_DRAFT           <- no SGR 2 anywhere

`tmux capture-pane -p` (no `-e`) **strips every SGR attribute**, so both
render as the byte-identical plain string ``❯ <text>``. Every consumer in
this project that asked "is a prompt still sitting in the composer?" --
adapters._sent_text_echoed, core._extract_composer_text,
core._codex_draft_in_composer -- and every human/orchestrator reading
terminal_tail was therefore structurally unable to tell "my prompt was
never submitted" from "the composer is empty and Claude is merely
suggesting a reply".

The live incident this produced, end to end, verified against the real
audit log and the real panes:
  1. Claude finishes a turn; composer is EMPTY; Claude draws a dim ghost
     suggestion into it.
  2. An operator/orchestrator reads the pane, sees ``❯ yes, publish the
     report``, and concludes its prompt is stuck unsubmitted.
  3. It calls terminal_send_keys(["Enter"]). Enter reaches a genuinely
     EMPTY composer, so Claude correctly does nothing at all and emits
     zero bytes.
  4. The pane is byte-identical before/after, so the verifier reports
     DELIVERY_UNKNOWN / SUBMIT_UNCONFIRMED -- which *reads* like "Enter
     was swallowed" and reinforces the false belief at step 2.

Proof it is the dim attribute and nothing else (real panes, disposable
Claude session, 2026-09-10/11):
  * ``tmux capture-pane -p -e`` on the two stuck live panes returned
    ``ESC[39m❯\xa0ESC[2m<text>ESC[0m`` -- dim.
  * Typing one character into hp1 re-rendered the row as ``❯ X``: the
    composer really was empty, the "pending prompt" was ghost text.
  * A backspace back to empty made the ghost text reappear.
  * On a disposable session a genuinely typed draft captured with `-e`
    carried NO SGR 2, and became dim again the moment C-u cleared it.

So: dim-vs-not is the deterministic discriminator between "there is
something to submit" and "there is nothing to submit", and it is only
visible in an ANSI-preserving capture.

Everything here is a pure function of captured text -- no tmux/ConPTY
call, no timing, no I/O -- so it is exhaustively unit-testable against
fixture rows and behaves identically for the local tmux path and the
remote Linux node path (both call TmuxClient.capture_lines).

Windows/ConPTY honesty: windows_backend.capture_lines documents `ansi`
as a deliberate no-op (pyte has already resolved SGR away, and pyte does
not model SGR 2 at all). read_composer therefore returns
COMPOSER_UNKNOWN there rather than guessing -- callers must degrade to
"uncertain", never to a false NOT_ACTIVATED or a false SUBMIT_CONFIRMED.
That degradation is detected from the capture itself (no SGR present at
all in a TUI capture), so no backend flag has to be plumbed through.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

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
_MARKER_CHARS = ("❯", "›", ">")
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
                # An empty parameter list (ESC[m) means SGR 0.
                for part in (body.split(";") if body else ["0"]):
                    # A colon-subparameter form (e.g. 38:2:...) can never
                    # be 2/22/0 itself; take the leading number only.
                    head = part.split(":")[0].strip()
                    if head in ("", "0"):
                        is_faint = False
                    elif head == "2":
                        is_faint = True
                    elif head == "22":
                        is_faint = False
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
