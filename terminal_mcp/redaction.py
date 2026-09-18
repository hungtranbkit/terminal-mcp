from __future__ import annotations

import re
from typing import Any


# Name half of a secret-bearing assignment: a WHOLE identifier that *ends*
# in a secret-ish suffix, not a bare `\b`-delimited keyword. `\b` never
# matches between `_` and a letter, so the original `\b(api[-_]?key|...)`
# and `\b(password|token)` rules silently missed every prefixed form --
# OPENROUTER_API_KEY=..., ANTHROPIC_AUTH_TOKEN=..., GITHUB_TOKEN=... --
# while still redacting the unprefixed `api_key=...`. That was the live
# gap: an OpenRouter key pasted as `export OPENROUTER_API_KEY=sk-or-...`
# survived redaction and could be captured verbatim into session_knowledge.
#
# Any number of `WORD_`/`WORD-` prefix segments is allowed, bounded to
# keep the regex cheap: the separator is excluded from a segment's body,
# so a name decomposes into segments exactly one way (no ambiguity to
# backtrack over), and both the repetition and the segment length are
# capped, so the worst case per start position is a small constant --
# never catastrophic backtracking on a large pane capture.
_SECRET_NAME = (
    r"(?:[A-Za-z0-9]{1,64}[_\-]){0,8}"
    r"(?:API[_\-]?KEYS?"
    r"|API[_\-]?TOKEN|AUTH[_\-]?TOKEN|ACCESS[_\-]?TOKEN"
    r"|SESSION[_\-]?TOKEN|REFRESH[_\-]?TOKEN|BEARER[_\-]?TOKEN"
    r"|SECRET[_\-]?KEY|ACCESS[_\-]?KEY|CLIENT[_\-]?SECRET"
    r"|PASSWORD|PASSWD|PASSPHRASE|TOKEN)"
)
# Value half. Quoted forms are matched explicitly -- `KEY="sk-or-..."` is
# the single most common shell shape and the old `[^\s'\";]+` value class
# stopped dead at the opening quote, leaving the secret in the clear.
_SECRET_VALUE = r"(?:'[^'\r\n]*'|\"[^\"\r\n]*\"|[^\s'\";]+)"
# `[ \t]`, never `\s`: a dangling `TOKEN =` at end of line must not reach
# across the newline and swallow the next line of surrounding pane output.
_ASSIGN = r"[ \t]*=[ \t]*"


REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Secret-bearing assignments -- `export FOO_API_KEY = <value>`,
    # `password=...`, `aws_session_token=...`, `--api-key=...`. Supersedes
    # the earlier separate OPENAI_API_KEY/ANTHROPIC_API_KEY, password|token,
    # aws_secret_access_key, aws_session_token and api_key|access_key|
    # client_secret|secret_key rules, all of which this one subsumes (each
    # still has its own regression test). `export` and any un-consumed
    # prefix simply stay outside the match, so they survive verbatim.
    (re.compile(r"(?i)(" + _SECRET_NAME + r")" + _ASSIGN + _SECRET_VALUE),
     r"\1=<REDACTED>"),

    (re.compile(r"(?i)\b(Bearer)\s+([A-Za-z0-9._~+\-/]+=*)"), r"\1 <REDACTED>"),
    (re.compile(r"(?im)\b(Authorization\s*:\s*)([^\r\n]+)"), r"\1<REDACTED>"),

    # URL query secrets first. The rule below has to allow `&` in its value
    # (a password may contain one, and half-redacting a password is worse
    # than over-redacting), which meant `?token=abc&page=2&limit=50` matched
    # whole and the benign parameters were destroyed along with the secret.
    # Found 2026-09-13 while hardening tail output; pre-existing.
    (re.compile(r"(?i)([?&](?:token|access_token|api[-_]?key|key|secret|password|passwd|"
                r"auth|sig|signature|session)\b)=([^&\s\"'#]+)"), r"\1=<REDACTED>"),


    # Bare vendor key shapes -- no variable name required, so a secret
    # cannot survive merely by appearing on its own (a bare paste, a curl
    # line, a config dump). Each prefix is fixed and distinctive, the same
    # posture as the GitHub/AWS/npm shapes below: `sk-or-...` (OpenRouter),
    # `sk-ant-...` (Anthropic), `sk-proj-...` (OpenAI project keys).
    (re.compile(r"\bsk-(?:or|ant|proj)-[A-Za-z0-9_\-]{15,255}"), "<REDACTED>"),


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
    # no such shape, so those are only caught assignment-shaped, by the
    # rule at the top.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<REDACTED>"),

    # npm publish tokens -- fixed prefix.
    (re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), "<REDACTED>"),

    # Cookies / session headers -- the whole value, since a session
    # cookie's value *is* the credential (no sub-parsing needed/safe to
    # assume).
    (re.compile(r"(?im)\b(Set-Cookie\s*:\s*)([^\r\n]+)"), r"\1<REDACTED>"),
    (re.compile(r"(?im)\b(Cookie\s*:\s*)([^\r\n]+)"), r"\1<REDACTED>"),


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



# ---------------------------------------------------------------------------
# Hardened output redaction (2026-09-13).
#
# WHY: a real terminal_tail was refused by a safety layer because its output
# carried a one-time bootstrap password. The REDACTIONS above only ever
# matched `KEY = VALUE` plus a fixed set of vendor token shapes, so
# `Password: hunter2`, `one-time password is hunter2`, a URL with `?token=`
# and a JWT all travelled in clear. The whole request was then blocked --
# the worst outcome available: the operator loses the entire tail AND the
# secret was in it anyway.
#
# Two rules shape everything below.
#
#   Redact the VALUE, keep the LINE. An operator debugging a failed bootstrap
#   needs to see that a one-time password was printed, which credential file
#   it went to, and what the surrounding error said. Dropping the line teaches
#   them nothing; dropping the value costs them nothing.
#
#   A label is not a secret. `must_change_password=true`, `/app/password` and
#   `PASSWORD_FILE=/etc/x` contain no credential. Redacting those is how a
#   team learns the redactor is noise and starts asking for it to be turned
#   off -- so boolean and placeholder values are explicitly preserved.
# ---------------------------------------------------------------------------

REDACTED = "<REDACTED>"

# Values that are labels, not credentials. A password field set to `true` is
# a flag; redacting it is pure noise.
BENIGN_VALUES = frozenset({
    "true", "false", "yes", "no", "none", "null", "nil", "unset", "unchanged",
    "required", "0", "1", "-", "n/a", "na", "***", "<redacted>", "redacted",
    "changed", "ok", "enabled", "disabled", "empty", "set", "provided",
})

# Credential labels in `label: value` / `label = value` form. The colon form
# is the half the original list missed entirely, and is how nearly every CLI
# and log line actually prints one.
_SECRET_LABEL = (
    r"password|passwd|passphrase|secret|token|credential|api[-_ ]?key|"
    r"access[-_ ]?key|private[-_ ]?key|auth[-_ ]?token|access[-_ ]?token|"
    r"refresh[-_ ]?token|session[-_ ]?key|client[-_ ]?secret|"
    r"one[-_ ]?time[-_ ]?password|otp|recovery[-_ ]?code|totp"
)

# Files whose CONTENT is a credential. The PATH stays visible -- an operator
# needs to know which file to look at -- and the content is never read.
CREDENTIAL_FILE_NAMES = (
    "webauth-bootstrap.txt", "bootstrap-credential", "bootstrap.txt",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", ".netrc", ".pgpass",
    "credentials.json", "service-account.json", "token.json", ".htpasswd",
)
_CREDENTIAL_FILE_RE = re.compile(
    r"(?i)(" + "|".join(re.escape(name) for name in CREDENTIAL_FILE_NAMES) + r")\b")


def _redact_labelled(match: "re.Match[str]") -> str:
    """Keep the label and separator, replace the value -- unless the value is
    a boolean/placeholder, which is a flag rather than a credential."""
    label, separator, value = match.group(1), match.group(2), match.group(3)
    if value.strip().strip("'\"").casefold() in BENIGN_VALUES:
        return match.group(0)
    return label + separator + REDACTED


def _redact_query(match: "re.Match[str]") -> str:
    return match.group(1) + "=" + REDACTED


# Ordered most-specific first. Each entry is (name, pattern, replacement) --
# the name is what telemetry counts, so an operator can see WHICH rule fired
# without anything ever logging the value it fired on.
HIGH_RISK_REDACTIONS: tuple[tuple[str, "re.Pattern[str]", Any], ...] = (
    # URL query secrets run FIRST, before the generic label rule below.
    # Ordered the other way, `labelled_secret` matched `token=abc&page=2`
    # whole -- its value class has to allow `&` so a password containing one
    # is not half-redacted -- and swallowed a benign query parameter with it.
    # Secrets in a URL query string -- `?token=`, `&api_key=`, `&sig=`.
    ("url_query_secret",
     re.compile(r"(?i)([?&](?:token|access_token|api[-_]?key|key|secret|password|passwd|"
                r"auth|sig|signature|session)\b)=([^&\s\"'#]+)"),
     _redact_query),

    # `Password: hunter2`, `one-time password = hunter2`, `OTP: 123456`.
    # The lookbehind keeps this rule out of URL query strings, which
    # `url_query_secret` above has already handled. Its value class has to
    # allow `&` so a password containing one is not half-redacted, and
    # without the lookbehind that same greed swallowed every remaining
    # query parameter after the first secret one.
    ("labelled_secret",
     re.compile(r"(?i)(?<![?&])\b(" + _SECRET_LABEL + r")(\s*[:=]\s*)([^\s,;)\]}\r\n]+)"),
     _redact_labelled),

    # "your one-time password is hunter2" / "temporary passcode: hunter2".
    ("phrased_secret",
     re.compile(r"(?i)\b((?:one[- ]time|temporary|initial|bootstrap|default|generated)\s+"
                r"(?:password|passcode|token|code))(\s+is\s+|\s*[:=]\s*)([^\s,;)\]}\r\n]+)"),
     _redact_labelled),

    # A JWT is three base64url segments and is always a credential.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
     REDACTED),

    # `https://user:password@host` -- the password half only.
    ("url_userinfo",
     re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+):([^/\s@]+)@"),
     r"\1:" + REDACTED + "@"),

    # Vendor shapes with unmistakable prefixes.
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), REDACTED),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"), REDACTED),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), REDACTED),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), REDACTED),
    ("openai_key", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}"), REDACTED),

    # `Basic <base64>` mid-line, e.g. inside an echoed curl command. The
    # header form is already covered by the Authorization rule above.
    ("basic_auth", re.compile(r"(?i)\b(Basic\s+)([A-Za-z0-9+/]{16,}={0,2})"),
     r"\1" + REDACTED),
)


def _apply(pattern: "re.Pattern[str]", replacement: Any, text: str) -> tuple[str, int]:
    """Substitute, counting only substitutions that actually CHANGED something.

    `re.subn` counts matches, not changes, so a rule re-matching text it had
    already redacted (`https://user:<REDACTED>@host`) reported a hit every
    time. That made the pipeline non-idempotent in its REPORT even though it
    was idempotent in its output -- and the controller re-redacts every
    federated response, so a clean payload would have been labelled as newly
    redacted on every hop.
    """
    changed = 0

    def _sub(match: "re.Match[str]") -> str:
        nonlocal changed
        produced = replacement(match) if callable(replacement) else match.expand(replacement)
        if produced != match.group(0):
            changed += 1
        return produced

    return pattern.sub(_sub, text), changed


def _values_hidden_by_escapes(text: str) -> list[str]:
    """Secret values that only become visible once ANSI escapes are stripped.

    A full-screen TUI colours its output constantly, so a printed password
    arrives as `ESC[1;32mpassword:ESC[0m ESC[31mhunter2ESC[0m` -- and every
    label rule fails, because the separator and the value are no longer
    adjacent. Matching against the stripped copy finds the value; removing
    that literal substring from the original keeps the escapes intact for the
    caller that asked for them.
    """
    stripped = strip_ansi(text)
    if stripped == text:
        return []
    found: list[str] = []
    for _name, pattern, replacement in HIGH_RISK_REDACTIONS:
        if replacement is not _redact_labelled:
            continue
        try:
            for match in pattern.finditer(stripped):
                value = match.group(3)
                if value and value.strip().strip("'\"").casefold() not in BENIGN_VALUES:
                    found.append(value)
        except Exception:  # noqa: BLE001 -- a scan failure is never fatal
            continue
    return found


def redact_output(text: str, *, ansi_safe: bool = False) -> tuple[str, dict[str, Any]]:
    """Redact secrets out of terminal output, and say what was redacted.

    NEVER raises and never returns nothing. If a rule misbehaves on
    pathological input it is skipped and recorded in `report["errors"]` -- a
    redactor that throws becomes a refused request, which is the exact
    failure this replaces, and costs the operator the safe 99% of the output
    along with the secret.

    Returns (redacted_text, report). The report counts rule hits by NAME and
    never contains a matched value, so it is safe to log, count and ship to
    telemetry.
    """
    report: dict[str, Any] = {"rules": {}, "errors": [], "high_risk": False,
                              "credential_files": 0, "redactions": 0}
    if not text:
        return text, report
    def _count(name: str, hits: int) -> None:
        if hits:
            report["rules"][name] = report["rules"].get(name, 0) + hits
            report["redactions"] += hits
            report["high_risk"] = True

    # The pre-existing REDACTIONS are applied through the SAME counting
    # helper rather than via redact_text, so their work shows up in the
    # report. It did not before, and the fixture that started all this
    # reported "1 secret removed" while the base rules had quietly removed
    # four more -- an operator reading that marker would have been told the
    # output was cleaner than it was.
    try:
        if ansi_safe:
            result = redact_ansi_safe(text)
            if result != text:
                _count("builtin_ansi_safe", 1)
        else:
            result = text
            for pattern, replacement in REDACTIONS:
                result, hits = _apply(pattern, replacement, result)
                _count("builtin", hits)
    except Exception as exc:  # noqa: BLE001 -- fall through to the hard rules
        report["errors"].append("base:" + type(exc).__name__)
        result = text

    for name, pattern, replacement in HIGH_RISK_REDACTIONS:
        try:
            result, hits = _apply(pattern, replacement, result)
        except Exception as exc:  # noqa: BLE001 -- one bad rule never kills a tail
            report["errors"].append(name + ":" + type(exc).__name__)
            continue
        _count(name, hits)

    # Second pass for values that escape sequences had split apart.
    try:
        for value in _values_hidden_by_escapes(result):
            if value in result:
                result = result.replace(value, REDACTED)
                _count("ansi_wrapped_secret", 1)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append("ansi_wrapped_secret:" + type(exc).__name__)

    files = len(_CREDENTIAL_FILE_RE.findall(result))
    if files:
        # The PATH stays -- it is how an operator finds the file. Only the
        # fact that one was referenced is flagged; content is never read.
        report["credential_files"] = files
        report["high_risk"] = True
    return result, report


def redaction_marker(report: dict[str, Any]) -> str | None:
    """A one-line, secret-free summary to append to redacted output.

    Silent redaction is its own bug: an operator who cannot tell "the command
    printed nothing" from "we removed it" goes looking in the wrong place.
    """
    if not report.get("redactions") and not report.get("credential_files"):
        return None
    parts = []
    if report.get("redactions"):
        rules = ", ".join("%s x%d" % (name, count) for name, count in
                          sorted(report.get("rules", {}).items()))
        parts.append("%d secret value(s) removed [%s]" % (report["redactions"], rules))
    if report.get("credential_files"):
        parts.append("%d credential file path(s) referenced -- contents never read "
                     "or returned" % report["credential_files"])
    if report.get("errors"):
        parts.append("%d rule(s) skipped" % len(report["errors"]))
    return "[REDACTED] " + "; ".join(parts)
