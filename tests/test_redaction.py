from terminal_mcp.redaction import redact_ansi_safe, redact_text, strip_ansi


def test_redacts_common_secrets():
    raw = "OPENAI_API_KEY=sk-live ANTHROPIC_API_KEY=ant-live\nBearer abc.def\nAuthorization: Basic xyz\npassword=hunter2 token=tok"
    result = redact_text(raw)
    for secret in ("sk-live", "ant-live", "abc.def", "Basic xyz", "hunter2", "token=tok"):
        assert secret not in result
    assert result.count("<REDACTED>") == 6


def test_preserves_normal_output():
    assert redact_text("BUILD STEP 1") == "BUILD STEP 1"


def test_strip_ansi_removes_sgr_sequences():
    colored = "\x1b[1;31mERROR\x1b[0m: build failed \x1b[32mOK\x1b[0m"
    assert strip_ansi(colored) == "ERROR: build failed OK"


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
