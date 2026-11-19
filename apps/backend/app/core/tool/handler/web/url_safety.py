"""web_fetch 的 URL 检查：交给 Firecrawl 之前，挡掉不该外传或不该抓的地址。

只做不联网的静态检查，不在本机解析域名：真正去抓的是 Firecrawl（默认在云端），本机 DNS 既不代表
它看到的地址，又会被本机代理改写（fake-ip 模式只返回占位地址），按它判断「是否内网」既保护不了什么、
又会误拦。自建在本机或局域网的 Firecrawl 被引去访问内网（含重定向）属于 AGENTS.md 待办「安全与权限」。

挡的是两类：会把凭据交给外部服务的 URL（账号密码、凭据型参数、疑似密钥），以及一眼就是本机 / 内网的
地址（localhost 名称、私有或保留的 IP 字面量，含 ``127.1`` 这类旧式 IPv4 写法）。
"""

import ipaddress
import socket
from urllib.parse import parse_qsl, unquote, urlsplit

from app.config.constant import Constant


def normalize_url(url: str) -> str:
    """去掉首尾空白；没写协议时补 ``https://``。"""

    normalized = url.strip()
    if urlsplit(normalized).scheme:
        return normalized
    return f"https://{normalized.lstrip('/')}"


def url_block_reason(url: str) -> str | None:
    """``url`` 须已经过 :func:`normalize_url`；可以抓时返回 ``None``，否则返回给模型的拦截原因。"""

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return "the URL scheme must be http or https"
    if parsed.username is not None or parsed.password is not None:
        return "the URL must not include credentials"
    host = parsed.hostname
    if not host:
        return "the URL must include a hostname"
    for name, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if name.casefold() in Constant.Web.SENSITIVE_QUERY_KEYS:
            return f"the URL contains the credential-like query parameter '{name}'"
    decoded = unquote(url)
    if Constant.Web.SECRET_VALUE_RE.search(decoded) or any(
        value.casefold().startswith("sk-")
        for _name, value in parse_qsl(urlsplit(decoded).query, keep_blank_values=True)
    ):
        return "the URL appears to contain an embedded secret"
    if host == "localhost" or host.endswith(".localhost") or _is_internal_ip_literal(host):
        return "the URL points to this machine or a private network"
    return None


def _is_internal_ip_literal(host: str) -> bool:
    """主机名是 IP 字面量且不可公网路由。``127.1``、``0x7f.0.0.1``、``2130706433`` 这类旧式 IPv4 写法
    ``ipaddress`` 不认，按 ``inet_aton`` 的规则解析（不联网）；普通域名解析不出来，按 ``False`` 处理。"""

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return False
    return address.is_multicast or not address.is_global
