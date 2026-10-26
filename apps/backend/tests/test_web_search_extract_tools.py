"""web_search / web_extract 工具及其 Firecrawl 客户端的单元测试。

无网络、不依赖真实 API Key：所有远端交互均通过 monkeypatch 的假 httpx.Client
或直接注入工具的假客户端完成，URL 安全校验通过注入 resolver 控制。

被测对象：
- app/core/tool/web/web_extract.py (WebExtractTool)
- app/core/tool/web/web_search.py (WebSearchTool)
- app/core/tool/web/firecrawl_client.py (FirecrawlClient、结果值对象与异常)
- app/core/tool/web/url_safety.py (URL 安全校验)
- app/core/tool/web/web_extract_args.py / web_search_args.py (参数模型)
"""

import logging
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.config.constant import Constant
from app.core.tool.handler.web import firecrawl_client
from app.core.tool.handler.web.firecrawl_client import (
    FirecrawlClient,
    FirecrawlRequestError,
    FirecrawlUnavailableError,
    WebExtractItem,
    WebSearchItem,
    _clean_metadata,
    _search_result_items,
)
from app.core.tool.handler.web.url_safety import (
    is_safe_public_url,
    normalize_url_for_request,
    sensitive_query_param_name,
    url_contains_secret,
)
from app.core.tool.handler.web_extract import WebExtractTool
from app.core.tool.handler.web_search import WebSearchTool
from app.core.tool.handler.web.web_extract_args import WebExtractArgs
from app.core.tool.handler.web.web_search_args import WebSearchArgs

# ---------------------------------------------------------------------------
# 假客户端 / 假 httpx 脚手架
# ---------------------------------------------------------------------------


class FakeClient:
    """可注入工具的假 Firecrawl 客户端：在不触发真实网络的情况下驱动工具层逻辑。

    通过回调在 ``search`` / ``extract`` 阶段返回受控结果，并记录调用次数与入参，
    以便断言「去重后只调用一次」「结果顺序与输入一致」等合同。
    """

    def __init__(
        self,
        *,
        search_items: list[WebSearchItem] | None = None,
        extract_factory=None,
        search_side_effect: BaseException | None = None,
        extract_side_effect: BaseException | None = None,
    ) -> None:
        self._search_items = search_items or []
        self._extract_factory = extract_factory
        self._search_side_effect = search_side_effect
        self._extract_side_effect = extract_side_effect
        self.search_calls: list[tuple[str, int]] = []
        self.extract_calls: list[tuple[list[str], str, int]] = []

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        self.search_calls.append((query, limit))
        if self._search_side_effect is not None:
            raise self._search_side_effect
        return self._search_items[:limit]

    def extract(
        self, urls: list[str], output_format: str, char_limit: int
    ) -> list[WebExtractItem]:
        self.extract_calls.append((list(urls), output_format, char_limit))
        if self._extract_side_effect is not None:
            raise self._extract_side_effect
        if self._extract_factory is not None:
            return self._extract_factory(urls, output_format, char_limit)
        return [
            WebExtractItem(
                url=url,
                title=f"title-{url}",
                content=f"content-{url}",
                metadata={"source": "fake"},
            )
            for url in urls
        ]


class _FakeFirecrawl(FirecrawlClient):
    """用可控的 ``_post`` 实现构造 FirecrawlClient，避免真实网络。

    以子类方法覆盖 ``_post``，确保同步调用与 ThreadPoolExecutor 工作线程内都能
    正确绑定 ``self``（直接对实例属性打补丁的裸函数不会被绑定，会导致丢参）。
    """

    def __init__(self, api_key: str = "dummy", post_impl=None) -> None:
        super().__init__(api_key=api_key)
        self._post_impl = post_impl or (lambda endpoint, body: {"success": True, "data": {}})

    def _post(self, endpoint: str, body: dict[str, object]) -> object:
        return self._post_impl(endpoint, body)


def _public_resolver(hostname: str) -> list[str]:
    """返回公开可路由地址的注入解析器（任何主机名都解析到 8.8.8.8）。"""
    return ["8.8.8.8"]


def _make_fake_httpx_client(
    payload: object,
    *,
    raise_on_status: bool = False,
    raise_on_post: BaseException | None = None,
    status_code: int = 200,
):
    """构造替换 ``httpx.Client`` 的假客户端工厂。

    ``payload`` 为 ``response.json()`` 的返回值；``raise_on_status`` 让
    ``raise_for_status`` 抛 ``httpx.HTTPStatusError``（模拟 HTTP 5xx）；
    ``raise_on_post`` 让 ``.post`` 直接抛异常（模拟网络层故障）；
    ``status_code`` 控制 ``response.status_code``，用于断言日志中的状态码字段。
    """

    class _FakeResponse:
        # status_code 在 __init__ 中绑定：类体不是闭包作用域，无法读取外层函数参数，
        # 但实例方法（函数）可以闭包捕获 status_code。
        def __init__(self) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if raise_on_status:
                raise httpx.HTTPStatusError(
                    "server error", request=None, response=None  # type: ignore[arg-type]
                )

        def json(self) -> object:
            return payload

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, json: Any = None, headers: Any = None) -> _FakeResponse:
            if raise_on_post is not None:
                raise raise_on_post
            return _FakeResponse()

    return _FakeClient


# ---------------------------------------------------------------------------
# 1. Firecrawl 搜索响应解析与 limit 钳制
# ---------------------------------------------------------------------------


def test_search_result_items_v2_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    """v2 结构 {success:true, data:{web:[...]}} 能被正确解析（潜在缺陷：结构兼容）。"""

    payload = {
        "success": True,
        "data": {
            "web": [
                {"title": "A", "url": "https://a.com", "description": "da"},
                {"title": "B", "url": "https://b.com"},
            ]
        },
    }
    items = _search_result_items(payload)

    assert [item["url"] for item in items] == ["https://a.com", "https://b.com"]
    assert items[0]["title"] == "A"


def test_web_tools_use_generic_minimal_ui_entries() -> None:
    """Web 工具只返回通用 entries，正文仍保留在模型 content 通道。"""

    client = FakeClient(
        search_items=[
            WebSearchItem(title="A", url="https://a.com", description="desc", position=1)
        ],
    )
    observation = WebSearchTool(client).execute(query="测试")

    assert observation.display_data == {
        "kind": "web-search-results",
        "query": "测试",
        "results": [{"title": "A", "url": "https://a.com"}],
    }
    assert observation.content is not None
    assert '"description":"desc"' in observation.content


def test_web_extract_ui_data_uses_generic_entries_without_document_content() -> None:
    """正文仍供模型使用，但 UI data 只使用通用 entries。"""

    client = FakeClient(
        extract_factory=lambda urls, fmt, cl: [
            WebExtractItem(
                url=urls[0],
                title="title",
                content="private document body",
                metadata={"source": "private metadata"},
            )
        ],
    )
    observation = WebExtractTool(client, resolver=_public_resolver).execute(
        urls=["https://example.com/docs"]
    )

    assert observation.display_data == {
        "kind": "web-extract-urls",
        "urls": [{"url": "https://example.com/docs"}],
    }
    assert "private document body" in observation.content
    assert "private metadata" not in str(observation.display_data)


def test_search_result_items_v1_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 结构 {success:true, data:[...]} 也能解析（潜在缺陷：legacy 兼容）。"""

    payload = {
        "success": True,
        "data": [
            {"title": "A", "url": "https://a.com"},
            {"title": "B", "url": "https://b.com"},
        ],
    }
    items = _search_result_items(payload)

    assert [item["url"] for item in items] == ["https://a.com", "https://b.com"]


def test_search_result_items_drops_entries_without_url() -> None:
    """缺 url 的项必须被丢弃，不能导致 KeyError（潜在缺陷：字段缺失健壮性）。"""

    payload = {
        "success": True,
        "data": {
            "web": [
                {"title": "has-url", "url": "https://a.com"},
                {"title": "no-url"},
                {"url": "https://c.com"},
            ]
        },
    }
    items = _search_result_items(payload)

    assert [item["url"] for item in items] == ["https://a.com", "https://c.com"]


def test_search_result_items_non_dict_payload_returns_empty() -> None:
    """payload 非 dict 时返回空列表而非抛异常（潜在缺陷：类型边界）。"""

    assert _search_result_items(None) == []
    assert _search_result_items("garbage") == []
    assert _search_result_items({"success": True, "data": "not-a-list"}) == []


def test_firecrawl_search_clamps_limit_to_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """limit 超过 Constant.Web.SEARCH_LIMIT_MAX 时，search 结果被钳制（潜在缺陷：越界保护）。"""

    many = [
        {"title": f"t{i}", "url": f"https://e.com/{i}"} for i in range(50)
    ]
    payload = {"success": True, "data": {"web": many}}
    client = _FakeFirecrawl(post_impl=lambda endpoint, body: payload)

    results = client.search("query", limit=1000)

    assert len(results) == Constant.Web.SEARCH_LIMIT_MAX
    assert all(isinstance(r, WebSearchItem) for r in results)


def test_firecrawl_search_parses_v1_via_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """通过假 _post 验证 v1 结构在 search() 路径下也能解析（潜在缺陷：端到端解析）。"""

    payload = {
        "success": True,
        "data": [{"title": "X", "url": "https://x.com", "markdown": "desc"}],
    }
    client = _FakeFirecrawl(post_impl=lambda endpoint, body: payload)

    results = client.search("q", limit=5)

    assert len(results) == 1
    assert results[0].url == "https://x.com"
    assert results[0].description == "desc"
    assert results[0].position == 1


# ---------------------------------------------------------------------------
# 2. HTTP 200 但 success:false 必须抛 FirecrawlRequestError
# ---------------------------------------------------------------------------


def test_post_success_false_raises_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 200 但 {success:false} 必须抛 FirecrawlRequestError（不得静默返回空）。"""

    fake_client = _make_fake_httpx_client(
        {"success": False, "error": "rate limited"}
    )
    monkeypatch.setattr(firecrawl_client.httpx, "Client", fake_client)

    client = FirecrawlClient(api_key="dummy")
    with pytest.raises(FirecrawlRequestError) as exc_info:
        client._post("search", {"query": "q"})

    assert "rate limited" in str(exc_info.value)


def test_search_propagates_success_false_as_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search() 在 success:false 时应向上抛 FirecrawlRequestError（潜在缺陷：绕过静默）。"""

    fake_client = _make_fake_httpx_client(
        {"success": False, "error": "upstream boom"}
    )
    monkeypatch.setattr(firecrawl_client.httpx, "Client", fake_client)

    client = FirecrawlClient(api_key="dummy")
    with pytest.raises(FirecrawlRequestError):
        client.search("q", limit=5)


def test_post_http_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 非 2xx（raise_for_status 抛错）应向上传播 httpx.HTTPError（潜在缺陷：错误透传）。"""

    fake_client = _make_fake_httpx_client({}, raise_on_status=True)
    monkeypatch.setattr(firecrawl_client.httpx, "Client", fake_client)

    client = FirecrawlClient(api_key="dummy")
    with pytest.raises(httpx.HTTPError):
        client._post("search", {"query": "q"})


def test_extract_empty_urls_returns_empty_list() -> None:
    """空 URL 列表直接返回空结果，不发起任何网络请求。"""

    client = FirecrawlClient(api_key="dummy")

    assert client.extract([], "markdown", 1000) == []


def test_post_transport_error_is_logged_and_propagated(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """传输层失败（client.post 直接抛 httpx.ConnectError）必须原样传播原始异常，并落带
    endpoint / status_code=None / error 含原因 / has_api_key=True 的 error 日志，且不泄漏
    api_key 原文（潜在缺陷：传输层失败未被捕获→NameError/UnboundLocalError、异常被包装或吞掉、
    status_code 误填、api_key 泄漏进日志）。"""

    api_key = "AKIA-TRANSPORT-TEST-SECRET"
    # 显式保存原始异常实例，用于验证 _post 传播的是「同一个」异常而非包装/替换。
    original_error = httpx.ConnectError("connect failed")
    fake_client = _make_fake_httpx_client({}, raise_on_post=original_error)
    monkeypatch.setattr(firecrawl_client.httpx, "Client", fake_client)
    client = FirecrawlClient(api_key=api_key)
    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    # 1) 必须抛原始 httpx.ConnectError，而非 NameError/UnboundLocalError 或包装异常。
    with pytest.raises(httpx.ConnectError) as exc_info:
        client._post("search", {"query": "q"})

    assert type(exc_info.value) is httpx.ConnectError
    assert str(exc_info.value) == "connect failed"
    # 同一实例被原样 raise，证明未被重新包装。
    assert exc_info.value is original_error

    failed = [r for r in caplog.records if r.msg == "firecrawl_api_call_failed"]
    assert failed, "传输层失败必须落 firecrawl_api_call_failed 日志"
    assert failed[0].data["endpoint"] == "search"
    # 2) 传输层失败拿不到响应对象，状态码必须是 None（而非某个真实 HTTP 码）。
    assert failed[0].data["status_code"] is None
    # 3) error 字段必须包含失败原因。
    assert "connect failed" in failed[0].data["error"]
    # 4) 必须标记是否携带 api_key，且为 True。
    assert failed[0].data["has_api_key"] is True
    # 5) 绝对不能把 api_key 原文写进任何日志。
    assert api_key not in _log_full_text(caplog.records)


def test_log_call_failure_direct_writes_structured_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_log_call_failure 直接调用时必须写 error 级结构化日志，含 endpoint / status_code / latency_ms
    / error / has_api_key，且绝不输出 api_key 原文（潜在缺陷：字段缺漏或错位、api_key 泄漏）。"""

    api_key = "AKIA-DIRECT-LOG-SECRET"
    client = FirecrawlClient(api_key=api_key)
    error = httpx.ConnectTimeout("timed out")
    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    # 直接驱动私有方法：传输层失败形态（status_code=None）。
    client._log_call_failure("scrape", None, 0.0, error)

    failed = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and r.msg == "firecrawl_api_call_failed"
    ]
    assert failed, "expected firecrawl_api_call_failed error record"
    assert failed[0].data["endpoint"] == "scrape"
    # 传输层失败形参 status_code 为 None 时，日志中必须保持 None。
    assert failed[0].data["status_code"] is None
    assert "timed out" in failed[0].data["error"]
    assert isinstance(failed[0].data["latency_ms"], int | float)
    assert "endpoint" in failed[0].data and "has_api_key" in failed[0].data
    assert failed[0].data["has_api_key"] is True
    # api_key 原文绝不应出现在任何日志中（仅 has_api_key 布尔）。
    assert api_key not in _log_full_text(caplog.records)


def test_post_unavailable_raises_unavailable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 api_key 与 base_url 时 _post 必须抛 FirecrawlUnavailableError（无网络）。"""

    client = FirecrawlClient(api_key="", base_url="")
    with pytest.raises(FirecrawlUnavailableError):
        client._post("search", {"query": "q"})


# ---------------------------------------------------------------------------
# 2b. Firecrawl 结构化日志（新增）：失败 error 级 / 成功 info 级 / 单页 warning，且不泄漏 api_key
# ---------------------------------------------------------------------------


def _log_full_text(records: list[logging.LogRecord]) -> str:
    """把所有日志记录拼成单一字符串，供「不出现 api_key 原文 / 不出现响应正文」断言。"""

    parts: list[str] = []
    for record in records:
        parts.append(record.getMessage())
        parts.append(repr(getattr(record, "data", None)))
        parts.append(str(getattr(record, "display_message", "")))
    return "\n".join(parts)


def test_post_http_error_logs_failed_event_with_fields(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """HTTP 非 2xx 时必须写 error 级 firecrawl_api_call_failed，data 含 endpoint/status_code/error
    （潜在缺陷：失败未结构化落盘，事后无法定位）。"""

    api_key = "AKIA-SUPER-SECRET-KEY-123"
    fake_client = _make_fake_httpx_client({}, raise_on_status=True, status_code=503)
    monkeypatch.setattr(firecrawl_client.httpx, "Client", fake_client)
    client = FirecrawlClient(api_key=api_key)
    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    with pytest.raises(httpx.HTTPError):
        client._post("search", {"query": "q"})

    failed = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and r.msg == "firecrawl_api_call_failed"
    ]
    assert failed, "expected firecrawl_api_call_failed error record"
    assert failed[0].data["endpoint"] == "search"
    assert failed[0].data["status_code"] == 503
    assert "error" in failed[0].data
    # api_key 原文绝不应出现在任何日志中（仅 has_api_key 布尔）。
    assert api_key not in _log_full_text(caplog.records)


def test_post_success_false_logs_reported_failure_event(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """HTTP 200 但 success:false 时必须写 error 级 firecrawl_api_reported_failure，data 含
    endpoint/status_code/error（潜在缺陷：业务失败被静默当成「0 条结果」）。"""

    api_key = "AKIA-SUPER-SECRET-KEY-123"
    fake_client = _make_fake_httpx_client(
        {"success": False, "error": "rate limited"}, status_code=200
    )
    monkeypatch.setattr(firecrawl_client.httpx, "Client", fake_client)
    client = FirecrawlClient(api_key=api_key)
    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    with pytest.raises(FirecrawlRequestError):
        client._post("scrape", {"url": "https://x.com", "formats": ["markdown"]})

    failed = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and r.msg == "firecrawl_api_reported_failure"
    ]
    assert failed, "expected firecrawl_api_reported_failure error record"
    assert failed[0].data["endpoint"] == "scrape"
    assert failed[0].data["status_code"] == 200
    assert failed[0].data["error"] == "rate limited"
    assert api_key not in _log_full_text(caplog.records)


def test_post_success_logs_info_and_not_response_body(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """成功路径写 info 级日志且不记录响应正文（潜在缺陷：日志泄露）。"""

    api_key = "AKIA-SUPER-SECRET-KEY-123"
    body_marker = "DO-NOT-LOG-MARKER-XYZ-987"
    payload = {
        "success": True,
        "data": {"url": "https://x.com", "markdown": body_marker, "metadata": {"title": "T"}},
    }
    fake_client = _make_fake_httpx_client(payload, status_code=200)
    monkeypatch.setattr(firecrawl_client.httpx, "Client", fake_client)
    client = FirecrawlClient(api_key=api_key)
    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    result = client._post("scrape", {"url": "https://x.com", "formats": ["markdown"]})

    assert result == payload
    succeeded = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and r.msg == "firecrawl_api_call_succeeded"
    ]
    assert succeeded, "expected firecrawl_api_call_succeeded info record"
    assert succeeded[0].data["endpoint"] == "scrape"
    assert succeeded[0].data["status_code"] == 200
    assert "latency_ms" in succeeded[0].data
    full_text = _log_full_text(caplog.records)
    assert body_marker not in full_text  # 响应正文不得落日志
    assert api_key not in full_text  # api_key 原文不得落日志


def test_scrape_one_page_failure_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_scrape_one 单页失败时写 warning 级 web_extract_page_failed，且 data 含失败 url（潜在缺陷：
    部分失败无日志，事后无法定位是哪一页因何失败）。"""

    api_key = "AKIA-SUPER-SECRET-KEY-123"
    bad_url = "https://e.com/bad"

    def _fail_post(endpoint: str, body: dict[str, object]) -> dict[str, object]:
        raise FirecrawlRequestError("boom on page")

    client = _FakeFirecrawl(api_key=api_key, post_impl=_fail_post)
    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    item = client._scrape_one(bad_url, "markdown", 100)

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.msg == "web_extract_page_failed"
    ]
    assert warnings, "expected web_extract_page_failed warning record"
    assert warnings[0].data["url"] == bad_url
    assert "boom on page" in warnings[0].data["error"]
    full_text = _log_full_text(caplog.records)
    assert api_key not in full_text
    # 单页失败不中断：返回带 error 的 item，不向上抛。
    assert item.error


# ---------------------------------------------------------------------------
# 3. 提取：char_limit / truncated / metadata 清洗 / 顺序
# ---------------------------------------------------------------------------


def test_scrape_applies_char_limit_and_truncated_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """content 超过 char_limit 时被截断且 truncated=True（潜在缺陷：截断语义）。"""

    long_content = "x" * 100
    payload = {
        "success": True,
        "data": {
            "url": "https://e.com/p",
            "markdown": long_content,
            "metadata": {"title": "T"},
        },
    }
    client = _FakeFirecrawl(post_impl=lambda endpoint, body: payload)

    items = client.extract(["https://e.com/p"], "markdown", char_limit=10)

    assert len(items) == 1
    assert items[0].content == "x" * 10
    assert items[0].truncated is True


def test_scrape_not_truncated_when_within_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """content 未超 char_limit 时 truncated 应为 False（潜在缺陷：误标截断）。"""

    payload = {
        "success": True,
        "data": {
            "url": "https://e.com/p",
            "markdown": "short",
            "metadata": {"title": "T"},
        },
    }
    client = _FakeFirecrawl(post_impl=lambda endpoint, body: payload)

    items = client.extract(["https://e.com/p"], "markdown", char_limit=100)

    assert items[0].truncated is False


def test_scrape_strips_metadata_noise_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """metadata 不应含 rawHtml/links/screenshot（潜在缺陷：体积/噪声泄漏）。"""

    payload = {
        "success": True,
        "data": {
            "url": "https://e.com/p",
            "markdown": "body",
            "metadata": {
                "title": "T",
                "rawHtml": "<html>",
                "links": ["a", "b"],
                "screenshot": "data:image/png;base64,xxx",
            },
        },
    }
    client = _FakeFirecrawl(post_impl=lambda endpoint, body: payload)

    items = client.extract(["https://e.com/p"], "markdown", char_limit=100)

    metadata = items[0].metadata
    assert "rawHtml" not in metadata
    assert "links" not in metadata
    assert "screenshot" not in metadata
    assert metadata.get("title") == "T"


def test_scrape_preserves_order_for_multiple_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多 URL 提取结果顺序必须与输入一致（潜在缺陷：并发保序）。"""

    urls = [f"https://e.com/{i}" for i in range(4)]

    def _fake_post(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True,
            "data": {
                "url": body["url"],
                "markdown": f"content-{body['url']}",
                "metadata": {"title": body["url"]},
            },
        }

    client = _FakeFirecrawl(post_impl=_fake_post)

    items = client.extract(urls, "markdown", char_limit=100)

    assert [item.url for item in items] == urls


def test_scrape_single_page_failure_records_error_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单页 _post 抛 FirecrawlRequestError 时该页带 error 且不中断其他页（潜在缺陷：容错）。"""

    good_url = "https://e.com/ok"
    bad_url = "https://e.com/bad"

    def _fake_post(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        if body["url"] == bad_url:
            raise FirecrawlRequestError("boom")
        return {
            "success": True,
            "data": {
                "url": body["url"],
                "markdown": "ok",
                "metadata": {},
            },
        }

    client = _FakeFirecrawl(post_impl=_fake_post)

    items = client.extract([good_url, bad_url], "markdown", char_limit=100)

    by_url = {item.url: item for item in items}
    assert by_url[good_url].error == ""
    assert "boom" in by_url[bad_url].error


def test_scrape_missing_data_object_records_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """响应缺少 data 对象时单页带 error 而非抛异常（潜在缺陷：结构异常健壮性）。"""

    client = _FakeFirecrawl(post_impl=lambda endpoint, body: {"success": True})

    items = client.extract(["https://e.com/p"], "markdown", char_limit=100)

    assert items[0].error  # 非空错误描述


def test_clean_metadata_removes_noise_and_handles_non_dict() -> None:
    """_clean_metadata 剔除噪声键；非 dict 输入返回空字典（潜在缺陷：纯函数边界）。"""

    assert _clean_metadata({"a": 1, "rawHtml": "<x>", "links": []}) == {"a": 1}
    assert _clean_metadata(None) == {}
    assert _clean_metadata("not-dict") == {}


# ---------------------------------------------------------------------------
# 4. WebExtractTool.execute：全失败 / 部分失败 / 去重
# ---------------------------------------------------------------------------


def test_extract_all_pages_fail_returns_error_retryable_true() -> None:
    """全部页失败时 execute 返回 status=error 且 retryable=True（潜在缺陷：重试语义）。"""

    failed = [
        WebExtractItem(
            url="https://e.com/1", title="", content="", metadata={}, error="boom1"
        )
    ]
    client = FakeClient(extract_factory=lambda urls, fmt, cl: failed)
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=["https://e.com/1"])

    assert obs.status == "error"
    assert "hint: this error can be retried" in obs.content
    assert "boom1" in obs.content


def test_extract_partial_success_marks_partial_and_failed_count() -> None:
    """部分失败时 status=success，payload 含 partial=True 与正确 failed_count。"""

    urls = ["https://e.com/1", "https://e.com/2", "https://e.com/3"]

    def _factory(req_urls, fmt, cl):
        return [
            WebExtractItem(url=urls[0], title="t", content="ok", metadata={}),
            WebExtractItem(url=urls[1], title="", content="", metadata={}, error="fail2"),
            WebExtractItem(url=urls[2], title="t", content="ok2", metadata={}),
        ]

    client = FakeClient(extract_factory=_factory)
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=urls)

    assert obs.status == "success"
    import json

    payload = json.loads(obs.content)
    assert "success" not in payload
    assert payload["partial"] is True
    assert payload["failed_count"] == 1

    results = payload["results"]
    # 成功项不应带 error / truncated 字段
    ok_item = next(r for r in results if r["url"] == urls[0])
    assert "error" not in ok_item
    assert "truncated" not in ok_item
    # 失败项带 error，无 content
    bad_item = next(r for r in results if r["url"] == urls[1])
    assert bad_item["error"] == "fail2"


def test_extract_full_success_no_partial_field() -> None:
    """全部成功时 payload 不应出现 partial / failed_count 字段。"""

    urls = ["https://e.com/1", "https://e.com/2"]

    def _factory(req_urls, fmt, cl):
        return [
            WebExtractItem(url=u, title="t", content="c", metadata={}) for u in req_urls
        ]

    client = FakeClient(extract_factory=_factory)
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=urls)
    import json

    payload = json.loads(obs.content)
    assert "partial" not in payload
    assert "failed_count" not in payload
    assert len(payload["results"]) == 2


def test_extract_dedup_requests_each_url_once() -> None:
    """重复 URL 在请求前去重，client.extract 仅被调用一次且 URL 唯一。"""

    client = FakeClient()
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=["https://e.com/a", "https://e.com/a", "https://e.com/a"])

    assert obs.status == "success"
    assert len(client.extract_calls) == 1
    called_urls = client.extract_calls[0][0]
    assert called_urls == ["https://e.com/a"]


def test_extract_url_count_over_limit_returns_error() -> None:
    """URL 数超过 Constant.Web.EXTRACT_URL_LIMIT_MAX 时返回确定性错误（潜在缺陷：越界保护）。"""

    urls = [f"https://e.com/{i}" for i in range(Constant.Web.EXTRACT_URL_LIMIT_MAX + 1)]
    client = FakeClient()
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=urls)

    assert obs.status == "error"
    assert "hint: this error can be retried" in obs.content


# ---------------------------------------------------------------------------
# 5. URL 安全
# ---------------------------------------------------------------------------


def test_url_safety_blocks_loopback_via_resolver() -> None:
    """回环地址（127.0.0.1）被拦截且错误信息以 'Blocked:' 开头。"""

    safe, reason = is_safe_public_url(
        "http://localhost/", resolver=lambda h: ["127.0.0.1"]
    )
    assert safe is False
    assert reason.startswith("Blocked:")


def test_url_safety_blocks_private_via_resolver() -> None:
    """内网地址（10.0.0.1）被拦截。"""

    safe, reason = is_safe_public_url(
        "http://host/", resolver=lambda h: ["10.0.0.1"]
    )
    assert safe is False
    assert "private" in reason or "internal" in reason


def test_url_safety_allows_public() -> None:
    """公开可路由地址通过校验。"""

    safe, reason = is_safe_public_url(
        "http://example.com/", resolver=lambda h: ["8.8.8.8"]
    )
    assert safe is True
    assert reason == ""


def test_url_safety_allows_proxy_fake_ip_resolution(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """主机名解析到代理 fake-ip 占位段（198.18.0.0/15）时放行，并写 web_url_fake_ip_allowed。

    潜在缺陷：把 fake-ip 占位地址当成内网 ⇒ 开启系统代理（Clash 系）的环境下所有域名
    都被拦，web_extract 完全不可用。放行必须留下可见日志，供事后审计这次的放宽。
    """

    caplog.set_level(logging.DEBUG, logger="coding_agent.backend")

    safe, reason = is_safe_public_url(
        "https://example.com/", resolver=lambda h: ["198.18.0.67"]
    )

    assert safe is True
    assert reason == ""
    allowed = [r for r in caplog.records if r.msg == "web_url_fake_ip_allowed"]
    assert allowed, "fake-ip 放行必须落 web_url_fake_ip_allowed 日志"
    assert allowed[0].data["host"] == "example.com"
    assert allowed[0].data["matched_address"] == "198.18.0.67"
    # 放行路径不得同时产生拦截日志（否则两者语义自相矛盾）。
    assert not [r for r in caplog.records if r.msg == "web_url_address_blocked"]


def test_url_safety_blocks_fake_ip_address_literal() -> None:
    """URL 主机名本身是 fake-ip 段的 IP 字面量时仍被拦截（字面量不经 DNS，判定可信）。"""

    safe, reason = is_safe_public_url(
        "http://198.18.0.67/", resolver=lambda h: ["198.18.0.67"]
    )

    assert safe is False
    assert reason.startswith("Blocked:")


def test_url_safety_blocks_when_fake_ip_mixed_with_private_address() -> None:
    """同一主机名既有 fake-ip 占位又有真实内网地址时，按保守方向拦截。"""

    safe, reason = is_safe_public_url(
        "https://example.com/", resolver=lambda h: ["198.18.0.67", "10.0.0.5"]
    )

    assert safe is False
    assert "private" in reason or "internal" in reason


def test_web_extract_succeeds_behind_fake_ip_proxy() -> None:
    """fake-ip 代理环境下 web_extract 能真正走到客户端（潜在缺陷：全域名被拦）。

    这是本次缺陷的端到端回归：解析结果为 fake-ip 占位段时必须照常提取，而不是在
    安全检查阶段就返回 Blocked。
    """

    client = FakeClient()
    tool = WebExtractTool(client, resolver=lambda h: ["198.18.0.67"])

    obs = tool.execute(urls=["https://example.com/page"])

    assert obs.status == "success"
    assert len(client.extract_calls) == 1


def test_url_safety_blocks_non_http_scheme() -> None:
    """非 http/https 协议被拦截。"""

    safe, reason = is_safe_public_url("ftp://example.com/", resolver=_public_resolver)
    assert safe is False
    assert reason.startswith("Blocked:")


def test_url_safety_blocks_userinfo_credentials() -> None:
    """带 userinfo 凭据的 URL 被拦截。"""

    safe, reason = is_safe_public_url(
        "http://user:pass@example.com/", resolver=_public_resolver
    )
    assert safe is False
    assert "credentials" in reason
    assert reason.startswith("Blocked:")


def test_url_safety_blocks_missing_hostname() -> None:
    """缺少主机名的 URL 被拦截。"""

    safe, reason = is_safe_public_url("http://", resolver=_public_resolver)
    assert safe is False
    assert reason.startswith("Blocked:")


def test_sensitive_query_param_detection() -> None:
    """token / api_key 等敏感查询参数名被识别（潜在缺陷：凭据泄漏拦截）。"""

    assert sensitive_query_param_name("https://e.com/?token=abc") == "token"
    assert sensitive_query_param_name("https://e.com/?API_KEY=xyz") == "API_KEY"
    assert sensitive_query_param_name("https://e.com/?q=normal") is None


def test_url_contains_secret_variants() -> None:
    """sk- / api_key= 等密钥形态被识别（潜在缺陷：密钥泄漏拦截）。"""

    assert url_contains_secret("https://e.com/?api_key=secret123") is True
    assert url_contains_secret("https://e.com/path/sk-abcdefghijklmnop") is True
    assert url_contains_secret("https://e.com/?q=hello") is False


def test_normalize_url_adds_https_when_scheme_missing() -> None:
    """缺协议的地址补充 https:// 前缀（潜在缺陷：默认协议）。"""

    assert normalize_url_for_request("example.com/path") == "https://example.com/path"
    assert normalize_url_for_request("https://example.com") == "https://example.com"


def test_web_extract_blocks_sensitive_query_param() -> None:
    """web_extract 对带 token 的 URL 返回 'Blocked:' 错误（潜在缺陷：端到端拦截）。"""

    client = FakeClient()
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=["https://example.com/?token=abc"])

    assert obs.status == "error"
    assert obs.content.startswith("error: Blocked:")


def test_web_extract_blocks_non_http_scheme() -> None:
    """web_extract 对非 http(s) 协议返回 'Blocked:' 错误。"""

    client = FakeClient()
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=["ftp://example.com/file"])

    assert obs.status == "error"
    assert obs.content.startswith("error: Blocked:")


def test_web_extract_blocks_userinfo() -> None:
    """web_extract 对带凭据的 URL 返回 'Blocked:' 错误。"""

    client = FakeClient()
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=["http://user:pass@example.com/"])

    assert obs.status == "error"
    assert obs.content.startswith("error: Blocked:")


# ---------------------------------------------------------------------------
# 6. 未配置时的可执行文案
# ---------------------------------------------------------------------------


def test_search_unconfigured_client_gives_actionable_message() -> None:
    """Firecrawl 未配置时，web_search 应给出含 FIRECRAWL_API_KEY 的可执行文案。"""

    client = FirecrawlClient(api_key="", base_url="")
    tool = WebSearchTool(client)

    obs = tool.execute(query="hello")

    assert obs.status == "error"
    assert "FIRECRAWL_API_KEY" in obs.content


def test_extract_unconfigured_client_gives_actionable_message() -> None:
    """Firecrawl 未配置时，web_extract 应给出 actionable 文案（潜在缺陷：可执行指引）。"""

    client = FirecrawlClient(api_key="", base_url="")
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=["https://example.com/"])

    assert obs.status == "error"
    assert "FIRECRAWL_API_KEY" in obs.content


# ---------------------------------------------------------------------------
# 7. 参数模型 schema
# ---------------------------------------------------------------------------


def test_web_extract_args_schema_constraints() -> None:
    """WebExtractArgs schema：items 为字符串、maxItems 对齐上限、format 不含 text。"""

    schema = WebExtractArgs.model_json_schema()
    urls_prop = schema["properties"]["urls"]

    assert urls_prop["items"] == {"type": "string"}
    assert urls_prop["maxItems"] == Constant.Web.EXTRACT_URL_LIMIT_MAX

    format_prop = schema["properties"]["format"]
    enum = format_prop.get("enum") or []
    assert "markdown" in enum
    assert "html" in enum
    assert "text" not in enum


def test_web_search_args_limit_max_equals_constant() -> None:
    """WebSearchArgs 的 limit 上限等于 Constant.Web.SEARCH_LIMIT_MAX。"""

    schema = WebSearchArgs.model_json_schema()
    limit_prop = schema["properties"]["limit"]

    assert limit_prop["maximum"] == Constant.Web.SEARCH_LIMIT_MAX


def test_web_extract_args_strict_rejects_type_mismatch() -> None:
    """strict 模式下类型不符应校验失败（潜在缺陷：禁止隐式转换）。"""

    with pytest.raises(ValidationError):
        WebExtractArgs(urls="not-a-list")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        WebExtractArgs(urls=["https://e.com"], format=123)  # type: ignore[arg-type]


def test_web_search_args_strict_rejects_type_mismatch() -> None:
    """WebSearchArgs strict 模式下 limit 传字符串应失败。"""

    with pytest.raises(ValidationError):
        WebSearchArgs(query="q", limit="5")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 8. 输出编码（ensure_ascii=False）
# ---------------------------------------------------------------------------


def test_web_search_chinese_not_escaped() -> None:
    """中文搜索结果序列化后不被转义为 \\uXXXX（潜在缺陷：中文乱码）。"""

    items = [
        WebSearchItem(title="中文标题", url="https://e.com", description="中文描述", position=1)
    ]
    client = FakeClient(search_items=items)
    tool = WebSearchTool(client)

    obs = tool.execute(query="中文")

    assert "\\u" not in obs.content
    assert "中文标题" in obs.content


def test_web_extract_chinese_not_escaped() -> None:
    """中文提取结果序列化后不被转义。"""

    def _factory(req_urls, fmt, cl):
        return [
            WebExtractItem(
                url=u, title="中文标题", content="中文正文内容", metadata={"k": "值"}
            )
            for u in req_urls
        ]

    client = FakeClient(extract_factory=_factory)
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=["https://e.com/1"])

    assert "\\u" not in obs.content
    assert "中文正文内容" in obs.content


# ---------------------------------------------------------------------------
# 9. 异常语义：未配置 vs 远端失败
# ---------------------------------------------------------------------------


def test_extract_unavailable_error_retryable_false() -> None:
    """客户端抛 FirecrawlUnavailableError（未配置）时工具返回 retryable=False。"""

    client = FakeClient(
        extract_side_effect=FirecrawlUnavailableError("not configured")
    )
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=["https://example.com/"])

    assert obs.status == "error"
    assert "hint: do not retry this tool call." in obs.content


def test_extract_request_error_retryable_true() -> None:
    """客户端抛 FirecrawlRequestError（远端失败）时工具返回 retryable=True。"""

    client = FakeClient(
        extract_side_effect=FirecrawlRequestError("remote failed")
    )
    tool = WebExtractTool(client, resolver=_public_resolver)

    obs = tool.execute(urls=["https://example.com/"])

    assert obs.status == "error"
    assert "hint: this error can be retried" in obs.content


def test_search_unavailable_error_retryable_false() -> None:
    """web_search 在 FirecrawlUnavailableError 时 retryable=False。"""

    client = FakeClient(
        search_side_effect=FirecrawlUnavailableError("not configured")
    )
    tool = WebSearchTool(client)

    obs = tool.execute(query="hello")

    assert obs.status == "error"
    assert "hint: do not retry this tool call." in obs.content


def test_search_request_error_retryable_true() -> None:
    """web_search 在 FirecrawlRequestError 时 retryable=True。"""

    client = FakeClient(
        search_side_effect=FirecrawlRequestError("remote failed")
    )
    tool = WebSearchTool(client)

    obs = tool.execute(query="hello")

    assert obs.status == "error"
    assert "hint: this error can be retried" in obs.content


# ---------------------------------------------------------------------------
# 工具定义装配
# ---------------------------------------------------------------------------


def test_build_definitions_return_tool_definition() -> None:
    """build_web_search_definition / build_web_extract_definition 返回可用定义（不触网）。"""

    from app.core.tool.handler.web_extract import build_web_extract_definition
    from app.core.tool.handler.web_search import build_web_search_definition

    search_def = build_web_search_definition()
    extract_def = build_web_extract_definition()

    assert search_def is not None and extract_def is not None
    assert search_def.name == "web_search"
    assert extract_def.name == "web_extract"
    assert search_def.args_model is WebSearchArgs
    assert extract_def.args_model is WebExtractArgs
