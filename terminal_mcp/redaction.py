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

