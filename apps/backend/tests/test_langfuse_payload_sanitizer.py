"""Tests for Langfuse outbound payload sanitization."""

import json

from app.core.observability.langfuse_payload_sanitizer import sanitize_langfuse_payload


def test_sanitize_langfuse_payload_redacts_nested_sensitive_keys() -> None:
    """Nested sensitive keys must be removed before payloads leave the process."""
    payload = {
        "arguments": {
            "Api_Key": "sk-secret-value-12345678901234567890",
            "nested": [{"Authorization": "Bearer token-value"}],
        },
        "message": "password: abcdef",
    }

    result = sanitize_langfuse_payload(payload)

    assert result["arguments"]["Api_Key"] == "[REDACTED]"
    assert result["arguments"]["nested"][0]["Authorization"] == "[REDACTED]"
    assert "[REDACTED]" in result["message"]


def test_sanitize_langfuse_payload_redacts_json_encoded_strings() -> None:
    """JSON strings used by OTel attributes should be parsed and redacted."""
    raw = json.dumps({"token": "secret-token", "content": "safe"}, ensure_ascii=False)

    result = sanitize_langfuse_payload(raw)

    parsed = json.loads(result)
    assert parsed == {"token": "[REDACTED]", "content": "safe"}


def test_sanitize_langfuse_payload_truncates_long_text() -> None:
    """Free-form text should be bounded to keep Langfuse payloads finite."""
    result = sanitize_langfuse_payload("x" * 9000)

    assert result.startswith("x" * 8000)
    assert result.endswith("...[TRUNCATED:9000]")
