from terminal_mcp.redaction import redact_text


def test_redacts_common_secrets():
    raw = "OPENAI_API_KEY=sk-live ANTHROPIC_API_KEY=ant-live\nBearer abc.def\nAuthorization: Basic xyz\npassword=hunter2 token=tok"
    result = redact_text(raw)
    for secret in ("sk-live", "ant-live", "abc.def", "Basic xyz", "hunter2", "token=tok"):
        assert secret not in result
    assert result.count("<REDACTED>") == 6


def test_preserves_normal_output():
    assert redact_text("BUILD STEP 1") == "BUILD STEP 1"
