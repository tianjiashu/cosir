"""Firecrawl 客户端：``web_search`` 与 ``web_extract`` 唯一的远端实现，两个工具直接调用。

只负责请求 Firecrawl 并把响应归一化为结果值对象；URL 安全校验、结果投影与输出预算归工具。

Firecrawl 官方当前 API 版本为 v2（v1 为 legacy）：搜索响应在 v2 下是 ``data.web``（数组嵌在
对象中），v1 下是 ``data`` 直接为数组，解析同时兼容。
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

from app.config.constant import Constant
from app.config.logging.logger import log
from app.utils.http_proxy import ProxyHttpClient

MISSING_CONFIGURATION_MESSAGE = (
    "Firecrawl is unavailable: set FIRECRAWL_API_KEY or FIRECRAWL_API_URL."
)


@dataclass(frozen=True, slots=True)
class WebSearchItem:
    """一条网页搜索结果。"""

    title: str
    url: str
    description: str
    position: int


@dataclass(frozen=True, slots=True)
class WebExtractItem:
    """一页网页正文提取结果。

    ``content`` 已按调用方的 ``char_limit`` 截断，``truncated`` 标记原文是否被裁剪，使模型能区分
    「页面就这么短」与「正文被截断」。
    """

    url: str
    title: str
    content: str
    metadata: dict[str, object]
    truncated: bool = False
    error: str = ""


class FirecrawlUnavailableError(RuntimeError):
    """本地缺少 Firecrawl 配置；重试无意义，工具应给出不可重试的配置指引。"""


class FirecrawlRequestError(RuntimeError):
    """配置正确但远端返回业务失败（HTTP 200 + success:false 等）；属瞬态故障，可以重试。"""


class FirecrawlClient:
    """通过 Firecrawl API 执行网页搜索与正文提取。"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        """读取 ``FIRECRAWL_API_KEY`` / ``FIRECRAWL_API_URL``（显式传入优先）。

        复用的同步 httpx 客户端由 ``ProxyHttpClient`` 持有，代理变化时自动重建，系统代理开关变化
        因此无需重启进程即对后续请求生效。
        """

        self._api_key = api_key if api_key is not None else os.environ.get("FIRECRAWL_API_KEY", "")
        self._base_url = (
            base_url if base_url is not None else os.environ.get("FIRECRAWL_API_URL", "")
        )
        self._proxy_http_client = ProxyHttpClient()

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """使用 Firecrawl API 执行网页搜索。

        参数:
            query: 搜索关键词。
            limit: 最多返回的搜索结果数量。

        返回:
            归一化后的网页搜索结果；远端无结果时返回空列表（属正常语义）。

        异常:
            FirecrawlUnavailableError: 缺少 Firecrawl 配置时抛出。
            FirecrawlRequestError: 远端返回 success:false 等业务失败时抛出。
            httpx.HTTPError: Firecrawl API 请求失败时抛出。

        副作用:
            发起 Firecrawl Search API 网络请求。
        """

        effective_limit = min(limit, Constant.Web.SEARCH_LIMIT_MAX)
        payload = self._post(
            "search",
            {"query": query, "limit": effective_limit, "sources": ["web"]},
        )
        return [
            WebSearchItem(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                description=str(item.get("description") or item.get("markdown") or ""),
                position=position,
            )
            for position, item in enumerate(
                _search_result_items(payload)[:effective_limit], start=1
            )
        ]

    def extract(
        self,
        urls: list[str],
        output_format: str,
        char_limit: int,
    ) -> list[WebExtractItem]:
        """并发调用 Firecrawl Scrape API 逐页提取网页正文。

        参数:
            urls: 待提取的网页地址列表。
            output_format: 请求 Firecrawl 返回的正文格式。
            char_limit: 每页正文最大保留字符数；超出部分截断并置 ``truncated``。

        返回:
            与 ``urls`` 顺序一致、归一化并截断到字符上限的正文结果；单页失败时
            该页结果带 ``error`` 且不中断其余页面。

        异常:
            FirecrawlUnavailableError: 缺少 Firecrawl 配置时抛出（不逐页吞掉，
                交由调用方给出不可重试的配置指引）。

        副作用:
            并发发起至多 ``Constant.Web.FIRECRAWL_MAX_CONCURRENT_SCRAPES`` 个 Firecrawl Scrape API 请求。
        """

        if not urls:
            return []
        worker_count = min(len(urls), Constant.Web.FIRECRAWL_MAX_CONCURRENT_SCRAPES)
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            return list(
                pool.map(
                    lambda url: self._scrape_one(url, output_format, char_limit),
                    urls,
                )
            )

    def _scrape_one(
        self,
        url: str,
        output_format: str,
        char_limit: int,
    ) -> WebExtractItem:
        """抓取单个 URL 并归一化为提取结果。

        参数:
            url: 待抓取的网页地址。
            output_format: 请求 Firecrawl 返回的正文格式。
            char_limit: 本页正文最大保留字符数。

        返回:
            成功时为带正文的提取结果；请求失败或响应结构异常时为带 ``error`` 的
            空结果（不影响同批次其他页面）。

        异常:
            FirecrawlUnavailableError: 缺少 Firecrawl 配置时向上抛出，不在本方法吞掉。

        副作用:
            发起一次 Firecrawl Scrape API 网络请求。
        """

        try:
            payload = self._post("scrape", {"url": url, "formats": [output_format]})
        except (httpx.HTTPError, FirecrawlRequestError) as exc:
            # 单页失败必须落日志：部分失败时上层返回 success，模型侧看不到堆栈，
            # 缺少此条记录将无法事后定位是哪一页、因何失败。
            log.warning(
                "web_extract_page_failed",
                extra={
                    "msg": "网页正文提取单页失败",
                    "data": {"url": url, "format": output_format, "error": str(exc)},
                },
            )
            return WebExtractItem(
                url=url,
                title="",
                content="",
                metadata={},
                error=f"Firecrawl scrape failed: {exc}",
            )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return WebExtractItem(
                url=url,
                title="",
                content="",
                metadata={},
                error="Firecrawl scrape response missing 'data' object.",
            )
        metadata = _clean_metadata(data.get("metadata")) or {
            key: value
            for key, value in data.items()
            if key not in Constant.Web.FIRECRAWL_EXTRACT_MAPPED_FIELDS
        }
        content = str(
            data.get(output_format) or data.get("markdown") or data.get("content") or ""
        )
        return WebExtractItem(
            url=str(data.get("url") or url),
            title=str(metadata.get("title", "")),
            content=content[:char_limit],
            truncated=len(content) > char_limit,
            metadata=metadata,
            error=str(data.get("error") or ""),
        )

    def _get_client(self) -> httpx.Client:
        """返回复用的同步 httpx 客户端（代理变化时自动重建）。

        委托 ``self._proxy_http_client``（``ProxyHttpClient``）持有缓存：每次取 client 都会重新
        解析一次系统代理，仅当解析到的代理与已缓存 client 所用代理不一致时才关闭旧 client 并重建，
        从而让系统代理开关变化在**不重启后端进程**的情况下对后续 web 请求实时生效。
        ``search`` / ``extract`` 共享同一实例。

        参数:
            无。

        返回:
            注入了当前系统代理（若启用）的 httpx.Client 实例。

        异常:
            无（构造失败由调用方 ``_post`` 的 httpx.HTTPError 路径统一处理）。

        副作用:
            代理变化时由 holder 关闭旧 client 并重建新 client。
        """

        return self._proxy_http_client.get(timeout=Constant.Web.REQUEST_TIMEOUT_SECONDS)

    def _post(self, endpoint: str, body: dict[str, object]) -> object:
        """向 Firecrawl API 发送 JSON 请求。

        参数:
            endpoint: Firecrawl API 相对端点。
            body: JSON 请求体。

        返回:
            Firecrawl 返回的 JSON 响应。

        异常:
            FirecrawlUnavailableError: 缺少 Firecrawl 配置时抛出。
            httpx.HTTPError: HTTP 请求失败（含传输层失败与非 2xx 状态码）时抛出。
            FirecrawlRequestError: HTTP 200 但远端返回 success:false 等业务失败时抛出。

        副作用:
            发起 Firecrawl API 网络请求；按成功 / 失败分别写 info / error 结构化日志
            （仅记录是否携带凭据，不输出 api_key 原文）。
        """

        if not (self._api_key or self._base_url):
            raise FirecrawlUnavailableError(MISSING_CONFIGURATION_MESSAGE)
        request_headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        base_url = self._base_url.rstrip("/") or Constant.Web.FIRECRAWL_DEFAULT_BASE_URL
        started = time.monotonic()
        try:
            response = self._get_client().post(
                f"{base_url}/{endpoint}", json=body, headers=request_headers
            )
        except httpx.HTTPError as exc:
            # 传输层失败（连接错误 / 超时 / 协议错误）拿不到响应对象，此处单独记录，
            # 否则最常见的网络故障将没有任何带 endpoint 与耗时的定位线索。
            self._log_call_failure(endpoint, None, started, exc)
            raise
        status_code = response.status_code
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            self._log_call_failure(endpoint, status_code, started, exc)
            raise
        payload = response.json()
        # HTTP 200 也可能携带 success:false（如远端超时、限流），必须转成显式失败，
        # 否则会被上层当成「查到了 0 条结果」而误导模型。
        if isinstance(payload, dict) and payload.get("success") is False:
            error_text = str(payload.get("error") or "Firecrawl API request failed.")
            log.error(
                "firecrawl_api_reported_failure",
                extra={
                    "msg": "Firecrawl API 返回业务失败",
                    "data": {
                        "endpoint": endpoint,
                        "status_code": status_code,
                        "latency_ms": round((time.monotonic() - started) * 1000, 2),
                        "error": error_text,
                    },
                },
            )
            raise FirecrawlRequestError(error_text)
        log.info(
            "firecrawl_api_call_succeeded",
            extra={
                "msg": "Firecrawl API 调用成功",
                "data": {
                    "endpoint": endpoint,
                    "status_code": status_code,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                },
            },
        )
        return payload

    def _log_call_failure(
        self,
        endpoint: str,
        status_code: int | None,
        started_at: float,
        error: Exception,
    ) -> None:
        """记录一次 Firecrawl 调用失败的结构化日志。

        参数:
            endpoint: 调用的 Firecrawl API 相对端点。
            status_code: HTTP 状态码；传输层失败拿不到响应时为 ``None``。
            started_at: ``time.monotonic()`` 记录的请求开始时刻。
            error: 传输层异常或非 2xx 状态码异常。

        返回:
            无。

        异常:
            无（仅构造日志，不参与失败传播）。

        副作用:
            写一条 error 级日志，含端点、状态码、耗时与失败原因；只记录是否携带
            凭据（``has_api_key``），绝不输出 api_key 原文。
        """

        log.error(
            "firecrawl_api_call_failed",
            extra={
                "msg": "Firecrawl API 调用失败",
                "data": {
                    "endpoint": endpoint,
                    "status_code": status_code,
                    "latency_ms": round((time.monotonic() - started_at) * 1000, 2),
                    "error": str(error),
                    "has_api_key": bool(self._api_key),
                },
            },
        )


def _search_result_items(payload: object) -> list[dict[str, Any]]:
    """从 Firecrawl 搜索响应中取出网页结果项。

    参数:
        payload: Firecrawl Search API 返回的 JSON 对象。

    返回:
        v2 结构（``data.web`` 为数组）与 v1 结构（``data`` 直接为数组）下均已归一
        化的结果字典列表；结构不符或无 ``url`` 的项被丢弃，缺失时返回空列表。

    异常:
        无。

    副作用:
        无（纯解析）。
    """

    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get("web")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("url")]


def _clean_metadata(metadata: object) -> dict[str, object]:
    """剔除 metadata 中体积大且对模型阅读无价值的键。

    参数:
        metadata: Firecrawl 返回的原始 metadata 对象。

    返回:
        去除 ``_METADATA_NOISE_KEYS`` 后的元数据副本；非字典输入返回空字典。

    异常:
        无。

    副作用:
        无（纯函数）。
    """

    if not isinstance(metadata, dict):
        return {}
    return {
        key: value
        for key, value in metadata.items()
        if key not in Constant.Web.FIRECRAWL_METADATA_NOISE_KEYS
    }
