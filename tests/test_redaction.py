from terminal_mcp.redaction import redact_ansi_safe, redact_text, strip_ansi


def test_redacts_common_secrets():
    raw = "OPENAI_API_KEY=sk-live ANTHROPIC_API_KEY=ant-live\nBearer abc.def\nAuthorization: Basic xyz\npassword=hunter2 token=tok"
    result = redact_text(raw)
    for secret in ("sk-live", "ant-live", "abc.def", "Basic xyz", "hunter2", "token=tok"):
        assert secret not in result
    assert result.count("<REDACTED>") == 6


def test_preserves_normal_output():
    assert redact_text("BUILD STEP 1") == "BUILD STEP 1"


# ---------------------------------------------------------------------------
# P0-10: expanded secret family coverage
# ---------------------------------------------------------------------------


def test_redacts_pem_private_key_block():
    raw = (
        "before\n-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdefgHIJKLMNOP\nQRSTUVWXYZ==\n"
        "-----END RSA PRIVATE KEY-----\nafter"
    )
    result = redact_text(raw)
    assert "MIIEpAIBAAKCAQEA1234567890abcdefgHIJKLMNOP" not in result
    assert "-----BEGIN RSA PRIVATE KEY-----" in result
    assert "-----END RSA PRIVATE KEY-----" in result
    assert "<REDACTED>" in result
    assert "before" in result and "after" in result


def test_redacts_plain_and_openssh_private_key_blocks():
    for key_type, header in (
        ("", "-----BEGIN PRIVATE KEY-----"),
        ("OPENSSH ", "-----BEGIN OPENSSH PRIVATE KEY-----"),
        ("EC ", "-----BEGIN EC PRIVATE KEY-----"),
    ):
        raw = f"{header}\nsecretbodycontenthere\n-----END {key_type}PRIVATE KEY-----"
        result = redact_text(raw)
        assert "secretbodycontenthere" not in result


def test_redacts_github_tokens():
    for token in ("ghp_" + "a" * 36, "gho_" + "b" * 36, "github_pat_" + "c" * 22):
        raw = f"export GITHUB_TOKEN={token}"
        result = redact_text(raw)
        assert token not in result
        assert "<REDACTED>" in result


def test_redacts_aws_keys():
    raw = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\naws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\naws_session_token=FQoGZXIvYXdzEXAMPLE"
    result = redact_text(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in result
    assert "wJalrXUtnFEMI" not in result
    assert "FQoGZXIvYXdzEXAMPLE" not in result


def test_redacts_npm_token():
    token = "npm_" + "x" * 36
    raw = f"//registry.npmjs.org/:_authToken={token}"
    assert token not in redact_text(raw)


def test_redacts_cookie_and_set_cookie_headers():
    raw = "Cookie: session_id=abc123def456; other=1\nSet-Cookie: sid=xyz789; HttpOnly"
    result = redact_text(raw)
    assert "abc123def456" not in result
    assert "xyz789" not in result
    assert "Cookie:" in result and "Set-Cookie:" in result


def test_redacts_generic_api_key_style_assignments():
    for assignment in ("api_key=abc123", "API-KEY=abc123", "access_key=abc123",
                       "client_secret=abc123", "secret_key=abc123"):
        result = redact_text(assignment)
        assert "abc123" not in result, assignment


def test_redacts_x_api_key_header():
    result = redact_text("X-Api-Key: super-secret-value-123")
    assert "super-secret-value-123" not in result


def test_redaction_does_not_over_redact_non_secret_near_misses():
    # Plain English/log lines that merely *mention* these words, with no
    # KEY=VALUE assignment or recognizable token shape, must survive
    # untouched -- this is the "minimize destructive over-redaction" half
    # of the requirement.
    benign = [
        "the secret to good code is testing",
        "this API key rotation policy runs monthly",
        "access is granted after login",
        "the cookie jar was empty",
        "npm install completed successfully",
        "github actions workflow finished",
        "client secret rotation reminder: next in 30 days",
    ]
    for line in benign:
        assert redact_text(line) == line, line


def test_strip_ansi_removes_sgr_sequences():
    colored = "\x1b[1;31mERROR\x1b[0m: build failed \x1b[32mOK\x1b[0m"
    assert strip_ansi(colored) == "ERROR: build failed OK"


# ---------------------------------------------------------------------------
# P0 hotfix (Windows terminal rendering): the original ANSI_CSI_RE only
# matched digits/`;` as CSI parameter bytes -- a DEC-private-mode sequence
# (parameter bytes include `?`) never matched at all and leaked through
# terminal_tail/terminal_status's "sanitized" output as raw, unreadable
# escape-code noise. Exactly the sequences a real full-screen TUI (Claude
# Code's own Ink renderer) emits constantly -- caught live via
# terminal_status(window) on dell-5530.
# ---------------------------------------------------------------------------

def test_strip_ansi_removes_dec_private_mode_cursor_visibility():
    assert strip_ansi("\x1b[?25lhidden\x1b[?25hvisible") == "hiddenvisible"


def test_strip_ansi_removes_alternate_screen_toggle():
    assert strip_ansi("\x1b[?1049hINSIDE ALT SCREEN\x1b[?1049l") == "INSIDE ALT SCREEN"


def test_strip_ansi_removes_bracketed_paste_mode():
    assert strip_ansi("\x1b[?2004hpasted text\x1b[?2004l") == "pasted text"


def test_strip_ansi_removes_osc_title_bel_terminated():
    assert strip_ansi("\x1b]0;My Window Title\x07after") == "after"


def test_strip_ansi_removes_osc_string_terminator_style():
    assert strip_ansi("\x1b]8;;http://example.com\x1b\\link text\x1b]8;;\x1b\\") == "link text"


def test_strip_ansi_removes_charset_designation():
    assert strip_ansi("\x1b(Bhello\x1b)0world") == "helloworld"


def test_strip_ansi_removes_simple_two_byte_escapes():
    # DEC keypad application/numeric mode, save/restore cursor (Fp escapes).
    assert strip_ansi("a\x1b=b\x1b>c\x1b7d\x1b8e") == "abcde"


def test_strip_ansi_preserves_wide_unicode_and_vietnamese_text():
    text = "Xin chào các bạn \x1b[?25lệ\x1b[?25h việt"
    assert strip_ansi(text) == "Xin chào các bạn ệ việt"


def test_strip_ansi_handles_a_real_captured_conpty_prefix():
    # Verbatim shape observed live from a real Windows ConPTY session
    # (dell-5530) -- the exact kind of content that used to leak through
    # terminal_status's own last_output field as raw escape noise.
    raw = "\x1b[?9001h\x1b[?1004h\x1b[?25lWindows PowerShell\x1b]0;title\x07\x1b[?25h"
    assert strip_ansi(raw) == "Windows PowerShell"


def test_redact_ansi_safe_catches_secret_glued_to_color_code():
    # Typical CLI styling: the escape code sits directly against the value,
    # with no whitespace in between.
    text = "password=\x1b[32mhunter2\x1b[0m ok"
    result = redact_ansi_safe(text)
    assert "hunter2" not in result
    assert "<REDACTED>" in result


def test_redact_ansi_safe_catches_secret_with_color_split_mid_token():
    # Adversarial layout: an escape code injected in the middle of what would
    # otherwise be one \S+ token, trying to dodge the plain-text regex.
    text = "OPENAI_API_KEY=sk-\x1b[31mlivesecretvalue1234567890\x1b[0m"
    result = redact_ansi_safe(text)
    assert "livesecretvalue1234567890" not in result
    assert "sk-" not in result
    assert "<REDACTED>" in result


def test_redact_ansi_safe_keeps_color_when_nothing_to_redact():
    text = "normal \x1b[32mgreen text\x1b[0m here"
    assert redact_ansi_safe(text) == text


def test_redact_ansi_safe_never_returns_fewer_redactions_than_plain():
    # Property check across several shapes: whatever redact_ansi_safe returns,
    # a plain-text scan of it must never find a secret that a plain-text scan
    # of the escape-stripped original would also have found and removed.
    cases = [
        "\x1b[31mpassword=hunter2\x1b[0m",
        "token=\x1b[1mtok\x1b[22m more text",
        "Authorization: \x1b[33mBearer abc.def\x1b[0m",
    ]
    for case in cases:
        plain = strip_ansi(case)
        redacted_plain = redact_text(plain)
        result = redact_ansi_safe(case)
        if redacted_plain != plain:
            assert result == redacted_plain
