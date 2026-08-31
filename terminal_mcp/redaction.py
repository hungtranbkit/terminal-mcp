from __future__ import annotations

import re


REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*=\s*([^\s'\";]+)"), r"\1=<REDACTED>"),
    (re.compile(r"(?i)\b(Bearer)\s+([A-Za-z0-9._~+\-/]+=*)"), r"\1 <REDACTED>"),
    (re.compile(r"(?im)\b(Authorization\s*:\s*)([^\r\n]+)"), r"\1<REDACTED>"),
    (re.compile(r"(?i)\b(password|token)\s*=\s*([^\s'\";]+)"), r"\1=<REDACTED>"),
)


def redact_text(text: str) -> str:
    result = text
    for pattern, replacement in REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI escape sequences (colour/style codes and similar)."""
    return ANSI_CSI_RE.sub("", text)


def redact_ansi_safe(text: str) -> str:
    """Redact text that may contain ANSI colour/style escape sequences.

    The REDACTIONS regexes above are proven against plain text; an escape
    code interleaved with a would-be secret is not guaranteed to keep every
    match boundary intact the same way. Fail safe: if stripping escapes and
    redacting the plain result would change anything, the coloured original
    is not provably safe to render as-is, so styling is dropped entirely for
    that render and the already-redacted plain text is returned instead of
    the unverifiable coloured one. Only when the plain view has nothing to
    redact is the (still separately redacted, as defense in depth) coloured
    text returned.
    """
    plain = strip_ansi(text)
    redacted_plain = redact_text(plain)
    if redacted_plain != plain:
        return redacted_plain
    return redact_text(text)

