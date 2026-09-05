from __future__ import annotations

import re


REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*=\s*([^\s'\";]+)"), r"\1=<REDACTED>"),
    (re.compile(r"(?i)\b(Bearer)\s+([A-Za-z0-9._~+\-/]+=*)"), r"\1 <REDACTED>"),
    (re.compile(r"(?im)\b(Authorization\s*:\s*)([^\r\n]+)"), r"\1<REDACTED>"),
    (re.compile(r"(?i)\b(password|token)\s*=\s*([^\s'\";]+)"), r"\1=<REDACTED>"),
    # P0-10 additions below. Each stays either (a) a recognizable,
    # high-confidence *token shape* (a vendor-specific prefix or fixed
    # structure unlikely to appear by chance in ordinary output -- GitHub/
    # AWS/npm token formats, a PEM block), or (b) assignment-shaped
    # (KEY = VALUE / Header: value), matching the existing password/token
    # precedent -- never a bare keyword like "secret" on its own, which
    # would over-redact plain English/log lines that merely mention the
    # word without ever containing a value to protect.

    # PEM private key material -- redact the whole body, keep the
    # BEGIN/END markers (and key type) so the fact that a key was present
    # is still visible without exposing it.
    (re.compile(r"-----BEGIN ((?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY)-----.*?-----END \1-----",
                re.DOTALL),
     r"-----BEGIN \1-----<REDACTED>-----END \1-----"),

    # GitHub tokens: classic (ghp_/gho_/ghu_/ghs_/ghr_) and fine-grained
    # (github_pat_) -- both are fixed, distinctive prefixes.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), "<REDACTED>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"), "<REDACTED>"),

    # AWS: access key IDs have a fixed, distinctive prefix (AKIA = long-
    # term, ASIA = temporary/STS); the secret key and session token have
    # no such shape, so only redact those when assignment-shaped (same
    # posture as password/token above).
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<REDACTED>"),
    (re.compile(r"(?i)\b(aws_secret_access_key)\s*=\s*([^\s'\";]+)"), r"\1=<REDACTED>"),
    (re.compile(r"(?i)\b(aws_session_token)\s*=\s*([^\s'\";]+)"), r"\1=<REDACTED>"),

    # npm publish tokens -- fixed prefix.
    (re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), "<REDACTED>"),

    # Cookies / session headers -- the whole value, since a session
    # cookie's value *is* the credential (no sub-parsing needed/safe to
    # assume).
    (re.compile(r"(?im)\b(Set-Cookie\s*:\s*)([^\r\n]+)"), r"\1<REDACTED>"),
    (re.compile(r"(?im)\b(Cookie\s*:\s*)([^\r\n]+)"), r"\1<REDACTED>"),

    # Generic API-key-like assignments not already covered by a specific
    # vendor pattern above -- same assignment-shaped, non-destructive
    # posture as password/token.
    (re.compile(r"(?i)\b(api[-_]?key|access[-_]?key|client[-_]?secret|secret[-_]?key)\s*=\s*([^\s'\";]+)"),
     r"\1=<REDACTED>"),
    (re.compile(r"(?im)\b(X-Api-Key\s*:\s*)([^\r\n]+)"), r"\1<REDACTED>"),
)


def redact_text(text: str) -> str:
    result = text
    for pattern, replacement in REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


# Full ECMA-48 CSI shape (real bug found live -- see windows_visible_
# console.py's report/task's own "P0 hotfix windows terminal rendering"):
# the pattern above only ever matched digits/`;` as parameter bytes, so a
# DEC-private-mode sequence (parameter bytes include `?`, e.g. `ESC[?25h`
# show-cursor, `ESC[?1049h` alternate-screen, `ESC[?2004h` bracketed-
# paste -- exactly the sequences a real, modern full-screen TUI like
# Claude Code's own Ink renderer emits constantly) never matched at all
# and leaked through terminal_tail/terminal_status's "sanitized" output
# as literal, unreadable escape-code noise. `[0-?]` covers the FULL
# parameter-byte range (0x30-0x3F: digits, `;:<=>?`), `[ -/]*` the
# intermediate-byte range (0x20-0x2F), `[@-~]` the final byte
# (0x40-0x7E) -- the complete CSI grammar, not just the common subset.
ANSI_CSI_FULL_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# OSC (Operating System Command) -- window title updates, hyperlinks
# (OSC 8), shell-integration markers (OSC 133/3008, seen live from this
# host's own bash prompt) -- terminated by BEL or ST (`ESC\`), never
# matched by CSI_RE at all (no `[` after ESC). Same shape dashboard.py's
# own frontend OSC_RE already strips client-side; this is the
# server-side/tool-output equivalent.
ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Simple two-byte Fp/Fe escapes with no CSI/OSC structure at all -- ESC
# followed by exactly ONE byte in the 0x30-0x3F/0x40-0x5F range (DEC
# keypad application/numeric mode `ESC=`/`ESC>`, index/next-line/save-
# restore-cursor `ESC7`/`ESC8`, ...); a real full-screen TUI emits these
# too. `[` and `]` are deliberately excluded from this range -- those
# start CSI/OSC, already fully matched and removed above; stripped last
# so this never runs against text a fuller sequence above should have
# consumed instead.
ANSI_SIMPLE_ESC_RE = re.compile(r"\x1b[0-9:;<=>?@-Z\\^_]")
# Character-set designation (`ESC ( B`, `ESC ) 0`, ...) -- three bytes,
# never caught by any of the above.
ANSI_CHARSET_RE = re.compile(r"\x1b[()][A-Za-z0-9]")


def strip_ansi(text: str) -> str:
    """Remove ANSI/VT control sequences -- CSI (including DEC private-mode
    parameters), OSC, charset designation, and simple two-byte escapes --
    so tail/status/history output is readable plain text, never a wall of
    raw escape codes from a full-screen TUI's own cursor/title/screen-mode
    control traffic."""
    text = ANSI_OSC_RE.sub("", text)
    text = ANSI_CSI_FULL_RE.sub("", text)
    text = ANSI_CHARSET_RE.sub("", text)
    text = ANSI_SIMPLE_ESC_RE.sub("", text)
    return text


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

