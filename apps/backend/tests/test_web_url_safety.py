"""Web URL safety boundary tests."""

from app.tools.tool_handler.web.url_safety import (
    extract_url_from_item,
    is_safe_public_url,
    normalize_url_for_request,
    sensitive_query_param_name,
    url_contains_secret,
)


def test_extract_url_accepts_string_and_search_result_objects() -> None:
    """验证 URL 提取支持字符串和常见搜索结果字典结构。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: URL 提取结果不符合预期时由断言抛出。

    副作用:
        无。
    """

    assert extract_url_from_item(" https://example.com/a ") == "https://example.com/a"
    assert extract_url_from_item({"url": "https://example.com/u"}) == "https://example.com/u"
    assert extract_url_from_item({"href": "https://example.com/h"}) == "https://example.com/h"
    assert extract_url_from_item({"href": 3}) is None


def test_normalize_adds_https_when_scheme_missing() -> None:
    """验证缺少协议的 URL 默认补充 HTTPS。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 规范化结果不符合预期时由断言抛出。

    副作用:
        无。
    """

    assert normalize_url_for_request("example.com/docs") == "https://example.com/docs"


def test_blocks_secret_like_url_values() -> None:
    """验证明文和编码后的疑似密钥值都会被识别。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 疑似密钥未被识别时由断言抛出。

    副作用:
        无。
    """

    assert url_contains_secret("https://example.com/?token=sk-test") is True
    assert url_contains_secret("https://example.com/?q=%73%6b-test") is True


def test_detects_credential_query_param_names() -> None:
    """验证凭据型查询参数名会被识别，普通参数不会被误判。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 查询参数敏感性判断不符合预期时由断言抛出。

    副作用:
        无。
    """

    assert sensitive_query_param_name("https://example.com/?api_key=abc") == "api_key"
    assert sensitive_query_param_name("https://example.com/?page=abc") is None


def test_rejects_private_ip_from_resolver() -> None:
    """验证解析到内部地址的 URL 会在 Provider 调用前被拒绝。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 内部地址未被拒绝时由断言抛出。

    副作用:
        使用内存解析器替身，不执行真实 DNS 查询。
    """

    safe, reason = is_safe_public_url(
        "https://metadata.google.internal/latest",
        resolver=lambda host: ["169.254.169.254"],
    )

    assert safe is False
    assert "private or internal" in reason


def test_rejects_cgnat_address_from_resolver() -> None:
    """验证解析到 CGNAT 共享地址的 URL 会被拒绝。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: CGNAT 共享地址未被拒绝时由断言抛出。

    副作用:
        使用内存解析器替身，不执行真实 DNS 查询。
    """

    safe, reason = is_safe_public_url(
        "https://shared-address.example",
        resolver=lambda _host: ["100.64.0.1"],
    )

    assert safe is False
    assert "private or internal" in reason


def test_rejects_url_with_authority_credentials() -> None:
    """验证 URL authority 中的用户名或密码会在 DNS 解析前被拒绝。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: authority 凭据未被拒绝时由断言抛出。

    副作用:
        使用内存解析器替身，不执行真实 DNS 查询。
    """

    safe, reason = is_safe_public_url(
        "https://user:password@example.com",
        resolver=lambda host: ["93.184.216.34"],
    )

    assert safe is False
    assert "credentials" in reason


def test_rejects_non_http_scheme() -> None:
    """验证非 HTTP(S) 协议会被拒绝。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 非 HTTP(S) 协议未被拒绝时由断言抛出。

    副作用:
        使用内存解析器替身，不执行真实 DNS 查询。
    """

    safe, reason = is_safe_public_url("file:///etc/passwd", resolver=lambda host: [])

    assert safe is False
    assert "http or https" in reason
