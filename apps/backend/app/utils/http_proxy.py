"""按配置解析 httpx 代理参数，并构建带系统代理的 httpx 客户端（不修改环境变量、不在启动时检测）。

本模块集中处理「外部请求走系统代理」的全部逻辑，避免在各 provider / 模型工厂里重复：

- ``resolve_httpx_proxy()``：仅当 ``CODING_AGENT_PROXY_AUTO_DETECT``（默认开启）为 true、
  且操作系统存在系统代理时，返回该代理 URL；否则返回 ``None``。
- ``build_proxy_client`` / ``build_proxy_async_client``：在构建 httpx 客户端时把解析到的系统代理
  注入 ``proxy=`` 参数；``resolve_httpx_proxy`` 返回 ``None`` 等价于不设置代理。
- ``ProxyHttpClient``：复用同一个同步 httpx 客户端的缓存 holder；每次取 client 都重新解析一次
  系统代理（亚毫秒级、不缓存于进程），仅当解析到的代理与已缓存 client 所用代理不一致时才重建，
  从而让系统代理的开关变化在**不重启进程**的情况下对后续请求实时生效。

返回 / 注入的代理 URL 是可直接传给 ``httpx.Client(proxy=...)`` 的字符串（如 ``http://127.0.0.1:7897``）。
调用方在「构建 http client 时」调用一次，由 httpx 负责把代理应用到该 client 的后续所有请求；
既不在进程启动时、也不在每次请求时重复检测系统代理。

设计边界：
- 唯一配置开关是 ``CODING_AGENT_PROXY_AUTO_DETECT``；不再支持在 ``.env`` / ``.env.local``
  手动写 ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY`` 来覆盖（旧方案已移除）。
- 只读取系统代理并返回 URL 字符串或构建 client，不读写任何环境变量。
- 不记录代理地址中的凭据；调用方也不得将代理 URL 写进日志。
- 该代理同时用于 http 与 https（同一本地代理如 Clash / V2RayN 的常见场景）。
- 注入代理的 client 面向外部主机（模型 API、Firecrawl）。若把 LLM ``base_url`` 指向
  ``localhost`` 等回环地址，需将 ``CODING_AGENT_PROXY_AUTO_DETECT`` 置为 false，避免回环流量被代理。
"""

from __future__ import annotations

import os
import urllib.request
from typing import Any

import httpx

__all__ = [
    "ProxyHttpClient",
    "build_proxy_async_client",
    "build_proxy_client",
    "resolve_httpx_proxy",
]

# 支持关闭自动检测的布尔值集合（与 Settings._env_bool 保持一致）。
_FALSE_LITERALS = {"0", "false", "no", "off"}


def resolve_httpx_proxy() -> str | None:
    """按 ``CODING_AGENT_PROXY_AUTO_DETECT`` 配置解析 httpx 代理 URL。

    解析顺序：
    1. 若 ``CODING_AGENT_PROXY_AUTO_DETECT`` 显式关闭（false/0/no/off）返回 ``None``；
    2. 读取操作系统代理（Windows 注册表 / macOS scutil / Linux 环境变量）；
    3. 优先取 https 代理，缺失时回退 http 代理；两者皆无则返回 ``None``；
    4. 返回清洗后的代理 URL 字符串（如 ``http://127.0.0.1:7897``）。

    参数:
        无。

    返回:
        可直接传给 ``httpx.Client(proxy=...)`` 的代理 URL 字符串；
        配置关闭或系统无代理时返回 ``None``（表示不走代理）。

    异常:
        无（读取系统代理失败静默返回 ``None``，不影响 client 构建）。

    副作用:
        无（只读系统配置，不修改环境变量、不创建 client）。
    """

    auto_detect = os.environ.get("CODING_AGENT_PROXY_AUTO_DETECT", "true").strip().lower()
    if auto_detect in _FALSE_LITERALS:
        return None
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    if not isinstance(proxies, dict):
        return None
    proxy_url = proxies.get("https") or proxies.get("http")
    if not isinstance(proxy_url, str) or not proxy_url.strip():
        return None
    return proxy_url.strip()


def build_proxy_client(**kwargs: Any) -> httpx.Client:
    """构建带系统代理的同步 ``httpx.Client``。

    在 ``kwargs`` 中未显式提供 ``proxy`` 时，自动以 ``resolve_httpx_proxy()`` 的解析结果注入；
    解析为 ``None`` 则等价于不设置代理（直连）。其余关键字参数原样透传给 ``httpx.Client``。

    参数:
        **kwargs: 透传给 ``httpx.Client`` 的构造参数（如 ``base_url`` / ``timeout``），
            可省略 ``proxy`` 以使用自动解析结果。

    返回:
        已注入系统代理（若启用）的 ``httpx.Client`` 实例。

    异常:
        无（构造失败由调用方的 httpx.HTTPError 路径统一处理）。

    副作用:
        构造一个 httpx 客户端（调用方负责在不再需要时关闭）。
    """

    if "proxy" not in kwargs:
        kwargs["proxy"] = resolve_httpx_proxy()
    return httpx.Client(**kwargs)


def build_proxy_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """构建带系统代理的异步 ``httpx.AsyncClient``。

    与 :func:`build_proxy_client` 同理，仅面向异步客户端。

    参数:
        **kwargs: 透传给 ``httpx.AsyncClient`` 的构造参数（如 ``base_url`` / ``timeout``），
            可省略 ``proxy`` 以使用自动解析结果。

    返回:
        已注入系统代理（若启用）的 ``httpx.AsyncClient`` 实例。

    异常:
        无（构造失败由调用方的 httpx.HTTPError 路径统一处理）。

    副作用:
        构造一个异步 httpx 客户端（调用方负责在不再需要时关闭）。
    """

    if "proxy" not in kwargs:
        kwargs["proxy"] = resolve_httpx_proxy()
    return httpx.AsyncClient(**kwargs)


class ProxyHttpClient:
    """复用同一个同步 httpx 客户端的缓存 holder，系统代理变化时自动重建。

    典型用法：在需要长期持有 client 的 provider 上维护一个实例，每次取 client 时调用
    :meth:`get` 并传入构造参数（如 ``timeout``）。该方法会重新解析一次系统代理，仅当解析到的
    代理与当前缓存 client 所用代理不一致时才关闭旧 client 并重建，从而实现代理设置实时生效。

    参数:
        无。

    异常:
        无。

    副作用:
        :meth:`get` 在代理变化时关闭旧 client 并构造新 client；:meth:`close` 显式释放资源。
    """

    def __init__(self) -> None:
        self._client: httpx.Client | None = None
        # 哨兵值：确保首次 get 一定构造（与 ``None`` 代理值区分开）。
        self._proxy: str | None = "_unset"

    def get(self, **kwargs: Any) -> httpx.Client:
        """返回当前代理对应的复用客户端；代理变化则重建。

        参数:
            **kwargs: 透传给 ``httpx.Client`` 的构造参数（如 ``timeout`` / ``base_url``）。
                ``proxy`` 由本方法按当前系统代理自动注入，调用方无需（也不应）显式传入。

        返回:
            注入了当前系统代理（若启用）的 ``httpx.Client`` 实例。

        异常:
            无（构造失败由调用方的 httpx.HTTPError 路径统一处理）。

        副作用:
            代理变化时关闭并重建 ``self._client``。
        """

        proxy = resolve_httpx_proxy()
        if self._client is None or self._proxy != proxy:
            if self._client is not None:
                self._client.close()
            self._client = build_proxy_client(**kwargs, proxy=proxy)
            self._proxy = proxy
        return self._client

    def close(self) -> None:
        """释放当前缓存的客户端（如有）。"""

        if self._client is not None:
            self._client.close()
        self._client = None
        self._proxy = "_unset"
