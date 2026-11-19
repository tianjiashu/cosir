"""Firecrawl 客户端：``web_search`` 与 ``web_fetch`` 唯一的远端实现，两个工具直接调用。

只负责请求 Firecrawl 并把响应归一化为结果值对象；URL 检查与结果渲染归工具。

配置（Key 与自建服务地址）每次请求现读 ``Settings``：配置中心保存后经 ``Settings.load`` 刷新，下一次调用
即生效，不用重启。没配置或 Key 被拒都抛 :class:`FirecrawlNotConfiguredError`，正文是给 Agent 的配置说明，
两个工具原样转交——工具一直在工具集合里，由 Agent 把怎么配置转告用户。

搜索只接受 v2 协议：网页结果位于 ``data.web``；旧版或错误响应直接报协议错误，不当成「没有结果」。
"""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx

from app.config.constant import Constant
from app.config.logging.logger import log
from app.config.settings import Settings
from app.utils.http_proxy import ProxyHttpClient

# 给 Agent 的配置说明：界面名称与配置中心一致（系统配置 → 环境变量 → 网页工具），用户照着就能找到。
_SETUP_STEPS = (
    "Tell the user how to set it up, then stop calling web_search and web_fetch until they say "
    "it is done:\n"
    "1. Get an API key at https://www.firecrawl.dev (sign up and copy the key from the "
    "dashboard). If they run their own Firecrawl server, they need its address instead.\n"
    '2. In this app, open 系统配置 → 环境变量 → 网页工具 and paste the key into "API Key" '
    '(or the server address into "服务地址").\n'
    "3. Save. It takes effect on the next call; no restart is needed."
)
_NOT_CONFIGURED = f"web search and fetch are not set up: they need a Firecrawl API key. {_SETUP_STEPS}"
_KEY_REJECTED = "the Firecrawl API key was rejected (HTTP {status}). " + _SETUP_STEPS


@dataclass(frozen=True, slots=True)
class WebSearchItem:
    """一条网页搜索结果。"""

    title: str
    url: str
    description: str


@dataclass(frozen=True, slots=True)
class WebFetchItem:
    """一页网页抓取结果；``truncated`` 区分「页面就这么短」与「正文按 ``char_limit`` 截断了」。"""

    url: str
    title: str = ""
    content: str = ""
    metadata: dict[str, object] | None = None
    truncated: bool = False
    error: str = ""


class FirecrawlNotConfiguredError(RuntimeError):
    """没配置或 Key 被拒：重试没有意义，正文是让 Agent 转告用户的配置说明。"""

    def __init__(self, message: str, *, status_hint: str) -> None:
        super().__init__(message)
        self.status_hint = status_hint


class FirecrawlRequestError(RuntimeError):
    """远端返回业务失败或响应不符合 v2 协议。"""


class FirecrawlClient:
    """通过 Firecrawl API 执行网页搜索与网页抓取；复用的 httpx 客户端随系统代理变化重建。"""

    def __init__(self) -> None:
        self._proxy_http_client = ProxyHttpClient()

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """``limit`` 已由参数模型限定在上限内；远端无结果时返回空列表。"""

        payload = self._post("search", {"query": query, "limit": limit, "sources": ["web"]})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("web"), list):
            raise FirecrawlRequestError("Firecrawl v2 search response requires a data.web array.")
        return [
            WebSearchItem(
                title=str(item.get("title") or ""),
                url=str(item["url"]),
                description=str(item.get("description") or ""),
            )
            for item in data["web"][:limit]
            if isinstance(item, dict) and item.get("url")
        ]

    def fetch(self, urls: list[str], output_format: str, char_limit: int) -> list[WebFetchItem]:
        """并发逐页抓取，结果顺序与 ``urls`` 一致；单页失败只让那一页带 ``error``，不中断其余页面。

        没配置或 Key 被拒对每一页都一样，所以不逐页吞掉，直接上抛给工具。
        """

        workers = min(len(urls), Constant.Web.FIRECRAWL_MAX_CONCURRENT_SCRAPES)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda url: self._scrape_one(url, output_format, char_limit), urls))

    def _scrape_one(self, url: str, output_format: str, char_limit: int) -> WebFetchItem:
        try:
            payload = self._post("scrape", {"url": url, "formats": [output_format]})
        except (httpx.HTTPError, FirecrawlRequestError, ValueError) as exc:
            # ValueError 是响应不是 JSON（网关或代理返回的 HTML 错误页）：只算这一页失败。
            # 部分失败时工具按成功返回，模型看不到堆栈，所以单页失败在这里落日志。
            log.warning(
                "web_fetch_page_failed",
                extra={
                    "msg": "网页抓取单页失败",
                    "data": {"url": url, "format": output_format, "error": str(exc)},
                },
            )
            return WebFetchItem(url=url, error=f"Firecrawl scrape failed: {exc}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return WebFetchItem(url=url, error="Firecrawl scrape response missing 'data' object.")
        raw_metadata = data.get("metadata")
        metadata = {
            key: value
            for key, value in (raw_metadata.items() if isinstance(raw_metadata, dict) else ())
            if key not in Constant.Web.FIRECRAWL_METADATA_NOISE_KEYS
        }
        content = Constant.Web.BASE64_IMAGE_PATTERN.sub(
            lambda match: f"[IMAGE: {match.group(1)}]", str(data.get(output_format) or "")
        )
        return WebFetchItem(
            url=str(data.get("url") or url),
            title=str(metadata.get("title") or ""),
            content=content[:char_limit],
            truncated=len(content) > char_limit,
            metadata=metadata,
            error=str(data.get("error") or ""),
        )

    def _post(self, endpoint: str, body: dict[str, object]) -> object:
        """发一次请求并返回 JSON；失败与成功都落结构化日志（只记是否带 Key，不记 Key 原文）。

        异常: ``FirecrawlNotConfiguredError``（没配置、401/403）、``httpx.HTTPError``（传输失败与其余
        非 2xx）、``FirecrawlRequestError``（HTTP 200 但 ``success: false``）、``ValueError``（响应不是 JSON）。
        """

        api_key, base_url = Settings.FIRECRAWL_API_KEY, Settings.FIRECRAWL_API_URL
        if not (api_key or base_url):
            raise FirecrawlNotConfiguredError(_NOT_CONFIGURED, status_hint="未配置网页工具")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        url = f"{(base_url.rstrip('/') or Constant.Web.FIRECRAWL_DEFAULT_BASE_URL)}/{endpoint}"
        started = time.monotonic()
        status_code: int | None = None
        try:
            client = self._proxy_http_client.get(timeout=Constant.Web.REQUEST_TIMEOUT_SECONDS)
            response = client.post(url, json=body, headers=headers)
            status_code = response.status_code
            if status_code in (401, 403):
                raise FirecrawlNotConfiguredError(
                    _KEY_REJECTED.format(status=status_code), status_hint="Key 无效"
                )
            response.raise_for_status()
            payload = response.json()
            # HTTP 200 也可能带 success:false（远端超时、限流）：转成显式失败，否则会被当成「0 条结果」。
            if isinstance(payload, dict) and payload.get("success") is False:
                raise FirecrawlRequestError(str(payload.get("error") or "Firecrawl API request failed."))
        except Exception as exc:
            log.error(
                "firecrawl_api_call_failed",
                extra={
                    "msg": "Firecrawl API 调用失败",
                    "data": {
                        "endpoint": endpoint,
                        "status_code": status_code,
                        "latency_ms": round((time.monotonic() - started) * 1000, 2),
                        "error": str(exc),
                        "has_api_key": bool(api_key),
                    },
                },
            )
            raise
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

