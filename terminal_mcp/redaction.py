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

