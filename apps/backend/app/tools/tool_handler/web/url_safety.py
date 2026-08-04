"""URL normalization and network safety checks for web extraction."""

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

SECRET_VALUE_RE = re.compile(
    r"(?i)(sk-[a-z0-9_-]{8,}|xox[baprs]-[a-z0-9-]{8,}|gh[pousr]_[a-z0-9_]{12,}|"
    r"api[_-]?key[=:][^&\s]+|bearer\s+[a-z0-9._-]{12,})"
)
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "code",
    "id_token",
    "jwt",
    "key",
    "password",
    "refresh_token",
    "secret",
    "session",
    "sig",
    "signature",
    "token",
}


def extract_url_from_item(value: object) -> str | None:
    """从字符串或搜索结果对象中提取非空 URL。

    参数:
        value: 候选 URL 字符串，或含有 ``url`` 或 ``href`` 字段的映射对象。

    返回:
        去除首尾空白后的 URL；输入不包含有效字符串 URL 时返回 ``None``。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, Mapping):
        return None

    for key in ("url", "href"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def normalize_url_for_request(url: str) -> str:
    """规范化待请求 URL，并为缺少协议的地址补充 HTTPS。

    参数:
        url: 模型或调用方提供的原始 URL。

    返回:
        去除首尾空白后的 URL；未识别到协议时以 ``https://`` 为前缀。

    异常:
        无。

    副作用:
        无。
    """

    normalized = url.strip()
    if urlsplit(normalized).scheme:
        return normalized
    return f"https://{normalized.lstrip('/')}"


def sensitive_query_param_name(url: str) -> str | None:
    """查找 URL 中第一个凭据型查询参数名。

    参数:
        url: 待检查的 URL，可包含百分号编码的查询参数名。

    返回:
        命中时返回原始参数名；未命中时返回 ``None``。

    异常:
        无。

    副作用:
        无。
    """

    query = urlsplit(url).query
    for name, _value in parse_qsl(query, keep_blank_values=True):
        if name.casefold() in SENSITIVE_QUERY_KEYS:
            return name
    return None


def url_contains_secret(url: str) -> bool:
    """判断 URL 文本及其百分号解码结果是否包含疑似密钥。

    参数:
        url: 待检查的原始 URL。

    返回:
        检测到常见密钥格式或 ``sk-`` 密钥前缀时返回 ``True``，否则返回 ``False``。

    异常:
        无。

    副作用:
        无。
    """

    decoded_url = unquote(url)
    return SECRET_VALUE_RE.search(decoded_url) is not None or any(
        value.casefold().startswith("sk-")
        for _name, value in parse_qsl(urlsplit(decoded_url).query, keep_blank_values=True)
    )


def is_safe_public_url(
    url: str,
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> tuple[bool, str]:
    """验证 URL 为无 authority 凭据的公开 HTTP(S) 主机。

    参数:
        url: 待校验的原始 URL。
        resolver: 可选的主机名解析函数；未提供时使用系统 DNS 解析。

    返回:
        安全时返回 ``(True, "")``；被拦截时返回 ``(False, "Blocked: ...")``。

    异常:
        无。URL 解析和 DNS 解析错误会被转换为拦截原因。

    副作用:
        未提供 ``resolver`` 时会执行系统 DNS 查询。
    """

    normalized_url = normalize_url_for_request(url)
    parsed = urlsplit(normalized_url)
    if parsed.scheme not in {"http", "https"}:
        return False, "Blocked: URL scheme must be http or https."
    if parsed.username is not None or parsed.password is not None:
        return False, "Blocked: URL must not include credentials."
    if not parsed.hostname:
        return False, "Blocked: URL must include a hostname."

    try:
        addresses = _resolve_host_addresses(parsed.hostname, resolver)
    except (OSError, ValueError):
        return False, "Blocked: hostname could not be resolved."

    if not addresses:
        return False, "Blocked: hostname could not be resolved."

    for address in addresses:
        try:
            ip_address = ipaddress.ip_address(address)
        except ValueError:
            return False, "Blocked: hostname resolved to an invalid address."
        if _is_private_or_internal_ip(ip_address):
            return False, "Blocked: URL resolves to a private or internal address."
    return True, ""


def _resolve_host_addresses(
    hostname: str,
    resolver: Callable[[str], Iterable[str]] | None,
) -> list[str]:
    """解析主机名的全部网络地址。

    参数:
        hostname: 待解析的 URL 主机名。
        resolver: 可选的注入解析函数。

    返回:
        主机名解析出的全部 IP 地址字符串。

    异常:
        OSError: 系统 DNS 查询失败时抛出。
        ValueError: 注入解析器提供非法值时可能由调用方抛出。

    副作用:
        未注入 ``resolver`` 时执行系统 DNS 查询。
    """

    if resolver is not None:
        return list(resolver(hostname))
    return [result[4][0] for result in socket.getaddrinfo(hostname, None)]


def _is_private_or_internal_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断 IP 地址是否属于禁止访问的内部或非全局路由范围。

    参数:
        address: 已成功解析的 IPv4 或 IPv6 地址。

    返回:
        地址为回环、私有、链路本地、多播、保留、未指定、站点本地或其他非全局可路由地址时
        返回 ``True``。

    异常:
        无。

    副作用:
        无。
    """

    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or getattr(address, "is_site_local", False)
        or not address.is_global
    )
