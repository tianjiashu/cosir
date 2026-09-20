"""URL normalization and network safety checks for web extraction."""

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from urllib.parse import parse_qsl, unquote, urlsplit

from app.config.logging.logger import log
from app.utils.http_proxy import resolve_httpx_proxy

# 单次拦截日志最多记录的解析地址数：多记录或双栈主机的解析结果可能很长，
# 无边界地写进日志字段会挤掉同一记录里的其它定位线索。
_MAX_LOGGED_ADDRESSES = 8

# 本地代理（Clash / Mihomo / Surge 等）fake-ip 模式的占位地址段（RFC2544 保留，永不公开分配）。
# 该模式下本机 DNS 只返回占位地址、真实解析发生在代理侧，因此这些地址不能用来判断「目标是
# 否属于内网」——把它当内网会让开启代理的环境阻断全部网页提取。它们也不会成为真实连接目标：
# 带代理时 httpx 通过 CONNECT 把主机名交给代理，本机根本不解析目标地址。
# 仅对「按主机名解析出来的地址」豁免；URL 里的 IP 字面量不经 DNS，仍按原规则拦截。
#
# 两处刻意选择（改动前先读）：
# 1) 用标准库 ``ip_network`` 表达网段，而不是复用 ``address.is_private`` / ``is_reserved``：
#    这里需要的是「把某一段从拦截集合中单独豁免」，标准库只提供布尔属性、无法表达豁免语义，
#    直接反判会把整个「保留/私有」集合一起放开。
# 2) 豁免**不**绑定「当前是否启用系统代理」：Clash 的 TUN 模式下系统代理可能未设置
#    （``getproxies()`` 返回 None）而 DNS 仍返回 fake-ip，绑上去会让 TUN 模式重新全量失效。
#    该段本身不可公网路由，无条件豁免的实际风险仅限「本机 DNS 被劫持到该段」，而那种情况下
#    连接同样无处可达；``auto_proxy`` 只作为日志字段用于事后审计。
_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")

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
        解析结果落在 ``_PROXY_FAKE_IP_NETWORK``（代理 fake-ip 占位段）且主机名不是 IP
        字面量时视为安全：该模式下本机 DNS 不携带真实目的地信息，判定交给代理出口策略。

    异常:
        无。URL 解析和 DNS 解析错误会被转换为拦截原因。

    副作用:
        未提供 ``resolver`` 时会执行系统 DNS 查询；主机名解析到回环、私有或其它非全局
        可路由地址时写一条 ``web_url_address_blocked`` 警告日志（含主机名、解析地址与
        当前系统代理状态），供事后区分真实 SSRF 尝试与本机 DNS/代理误判；命中 fake-ip
        占位段而放行时写 ``web_url_fake_ip_allowed`` 警告日志，使「守卫被放宽」在日志里可见。
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

    host_is_address_literal = _is_ip_literal(parsed.hostname)
    fake_ip_addresses: list[str] = []
    for address in addresses:
        try:
            ip_address = ipaddress.ip_address(address)
        except ValueError:
            return False, "Blocked: hostname resolved to an invalid address."
        if not host_is_address_literal and ip_address in _PROXY_FAKE_IP_NETWORK:
            # 代理 fake-ip 占位地址：本机 DNS 不等于真实目的地，不参与内网判定。
            # 此处只暂存、不写日志：日志必须在循环确认「没有任何地址触发拦截」之后才写，
            # 否则同批解析里若有真实内网地址、最终结论是拦截，URL 仍会留下一条语义为
            # 「已放宽」的放行记录，审计上会被误读为「该 URL 曾被放行」。
            fake_ip_addresses.append(address)
            continue
        if _is_private_or_internal_ip(ip_address):
            # 该分支不发起任何网络请求，模型侧只会看到一句 "Blocked"，因此必须落日志记录
            # 主机名、解析结果与当前是否启用系统代理：否则无法区分「真实 SSRF 尝试」与
            # 「本机 DNS/代理把公开域名解析成保留地址」导致的误拦。
            _log_address_decision(
                event="web_url_address_blocked",
                message="网页 URL 解析到禁止访问的地址，已拦截",
                host=parsed.hostname,
                addresses=addresses,
                matched_address=address,
            )
            return False, "Blocked: URL resolves to a private or internal address."
    if fake_ip_addresses:
        # 已确认放行才写：这条日志是「守卫被放宽」的审计证据，必须与最终结论严格一致。
        _log_address_decision(
            event="web_url_fake_ip_allowed",
            message="网页 URL 解析到代理 fake-ip 占位地址，跳过内网判定",
            host=parsed.hostname,
            addresses=addresses,
            matched_address=fake_ip_addresses[0],
        )
    return True, ""


def _is_ip_literal(hostname: str) -> bool:
    """判断 URL 主机名是否为标准记法的 IP 字面量。

    参数:
        hostname: ``urlsplit`` 解析出的主机名（IPv6 字面量已去除方括号）。

    返回:
        主机名本身就是 ``ipaddress`` 可解析的 IPv4/IPv6 地址时返回 ``True``，否则返回
        ``False``。非常规 IPv4 记法（``127.1`` / ``0x7f.0.0.1`` / ``2130706433`` 等）
        不被 ``ipaddress`` 识别，按 ``False`` 处理：这类写法会走域名分支，其拦截依赖
        系统解析器把等价地址规范化成回环地址后仍被判为内部地址（真实环境成立）。

    异常:
        无（不可解析时返回 ``False``）。

    副作用:
        无。
    """

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _log_address_decision(
    event: str,
    message: str,
    host: str,
    addresses: list[str],
    matched_address: str,
) -> None:
    """记录一次 URL 地址判定的结构化日志。

    参数:
        event: 稳定事件名（``web_url_address_blocked`` 或 ``web_url_fake_ip_allowed``）。
        message: 面向日志阅读者的中文说明。
        host: 被判定 URL 的主机名；只记主机名，不记 URL 原文与查询串。
        addresses: 本次解析出的全部地址，仅记录前 ``_MAX_LOGGED_ADDRESSES`` 条。
        matched_address: 触发本次判定的地址；它可能落在被截断的地址列表之外，故单独成字段。

    返回:
        无。

    异常:
        无。

    副作用:
        写一条 WARNING 级结构化日志，含主机名、解析地址、命中地址与当前系统代理状态
        （``auto_proxy`` 仅供事后审计，不参与判定）；不写 URL 原文、查询串与凭据。
    """

    log.warning(
        event,
        extra={
            "msg": message,
            "data": {
                "host": host,
                "addresses": addresses[:_MAX_LOGGED_ADDRESSES],
                "matched_address": matched_address,
                "auto_proxy": resolve_httpx_proxy() is not None,
            },
        },
    )


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
