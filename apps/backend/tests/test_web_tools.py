"""web_search / web_fetch 与 Firecrawl 客户端、URL 检查的单元测试；无网络，远端交互都由假 httpx 客户端驱动。"""

from __future__ import annotations

import json
import logging
import socket
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.config.constant import Constant
from app.config.settings import Settings
from app.core.tool.handler.web.firecrawl_client import (
    FirecrawlClient,
    FirecrawlNotConfiguredError,
    FirecrawlRequestError,
    WebFetchItem,
)
from app.core.tool.handler.web.url_safety import normalize_url, url_block_reason
from app.core.tool.handler.web.web_fetch_args import WebFetchArgs
from app.core.tool.handler.web.web_search_args import WebSearchArgs
from app.core.tool.handler.web_fetch import WebFetchTool, build_web_fetch_definition
from app.core.tool.handler.web_search import WebSearchTool, build_web_search_definition
from app.core.tool.schemas import ToolDefinition

_SETUP_PATH = "系统配置 → 环境变量 → 网页工具"


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Settings, "FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(Settings, "FIRECRAWL_API_URL", "")


class _Response:
    def __init__(self, payload: object, status_code: int = 200, raw: str | None = None) -> None:
        self._payload, self.status_code, self._raw = payload, status_code, raw

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.firecrawl.dev/v2/x")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("bad status", request=request, response=response)

    def json(self) -> object:
        if self._raw is not None:
            return json.loads(self._raw)
        return self._payload


def _client(respond) -> tuple[FirecrawlClient, list[dict[str, Any]]]:
    """``respond(endpoint, body)`` 返回 ``_Response``；记下每次请求的地址、请求体与请求头。"""

    calls: list[dict[str, Any]] = []

    def post(url: str, json: Any = None, headers: Any = None) -> _Response:
        calls.append({"url": url, "body": json, "headers": headers})
        return respond(url.rsplit("/", 1)[-1], json)

    client = FirecrawlClient()
    http = SimpleNamespace(post=post)
    client._proxy_http_client = SimpleNamespace(get=lambda timeout: http)  # type: ignore[assignment]
    return client, calls


def _scrape_ok(endpoint: str, body: dict[str, Any]) -> _Response:
    url = body["url"]
    data = {"url": url, "markdown": f"body of {url}", "html": f"<p>{url}</p>", "metadata": {"title": url}}
    return _Response({"success": True, "data": data})


# ---------------------------------------------------------------------------
# 配置：没配置、Key 被拒、配置现读
# ---------------------------------------------------------------------------


def test_unconfigured_tools_tell_the_agent_how_to_set_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配置时工具照样可调，返回给 Agent 转告用户的配置说明，不发请求。"""

    monkeypatch.setattr(Settings, "FIRECRAWL_API_KEY", "")
    client, calls = _client(_scrape_ok)

    search = WebSearchTool(client).execute(query="q")
    fetch = WebFetchTool(client).execute(urls=["https://example.com"])

    assert calls == []
    for observation in (search, fetch):
        assert observation.status == "error"
        assert "https://www.firecrawl.dev" in observation.content
        assert _SETUP_PATH in observation.content
        assert "Try" not in observation.content
        assert observation.display_data == {"status_hint": "未配置网页工具"}


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_gives_the_setup_steps_not_a_retry(status: int) -> None:
    client, _ = _client(lambda endpoint, body: _Response({}, status_code=status))

    observation = WebSearchTool(client).execute(query="q")

    assert observation.status == "error"
    assert f"rejected (HTTP {status})" in observation.content
    assert _SETUP_PATH in observation.content
    assert "Try" not in observation.content
    assert observation.display_data == {"status_hint": "Key 无效"}


def test_configuration_is_read_on_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置中心保存后（``Settings.load``）下一次请求即用新值，不用重建客户端。"""

    client, calls = _client(lambda endpoint, body: _Response({"success": True, "data": {"web": []}}))
    client.search("q", 1)
    monkeypatch.setattr(Settings, "FIRECRAWL_API_KEY", "")
    monkeypatch.setattr(Settings, "FIRECRAWL_API_URL", "http://127.0.0.1:3002/v2/")
    client.search("q", 1)

    assert calls[0]["headers"] == {"Authorization": "Bearer fc-test"}
    assert calls[0]["url"] == f"{Constant.Web.FIRECRAWL_DEFAULT_BASE_URL}/search"
    assert calls[1]["headers"] == {}
    assert calls[1]["url"] == "http://127.0.0.1:3002/v2/search"


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


def test_search_returns_results_in_order_and_card_lists_sources() -> None:
    payload = {
        "success": True,
        "data": {"web": [
            {"title": "甲", "url": "https://a.com", "description": "摘要"},
            {"title": "no url"},
            {"url": "https://b.com"},
        ]},
    }
    client, calls = _client(lambda endpoint, body: _Response(payload))

    observation = WebSearchTool(client).execute(query="测试", limit=5)

    assert calls[0]["body"] == {"query": "测试", "limit": 5, "sources": ["web"]}
    assert observation.status == "success"
    assert '"title":"甲","url":"https://a.com","description":"摘要"' in observation.content
    assert observation.display_data == {
        "kind": "web-search-results",
        "query": "测试",
        "results": [{"title": "甲", "url": "https://a.com"}, {"title": "", "url": "https://b.com"}],
    }


@pytest.mark.parametrize(
    "payload",
    [{"success": True, "data": [{"url": "https://a.com"}]}, {"success": True}, ["x"]],
)
def test_search_rejects_non_v2_responses(payload: object) -> None:
    client, _ = _client(lambda endpoint, body: _Response(payload))
    with pytest.raises(FirecrawlRequestError):
        client.search("q", 3)


def test_success_false_is_a_failure_not_zero_results() -> None:
    client, _ = _client(lambda endpoint, body: _Response({"success": False, "error": "rate limited"}))

    with pytest.raises(FirecrawlRequestError, match="rate limited"):
        client.search("q", 3)
    observation = WebSearchTool(client).execute(query="q")
    assert observation.status == "error"
    assert observation.content == "Web search failed. Try the search again."


def test_failed_request_is_logged_without_the_key(caplog: pytest.LogCaptureFixture) -> None:
    client, _ = _client(lambda endpoint, body: _Response({}, status_code=500))
    with caplog.at_level(logging.DEBUG), pytest.raises(httpx.HTTPStatusError):
        client.search("q", 3)
    failures = [record for record in caplog.records if "firecrawl_api_call_failed" in record.getMessage()
                or getattr(record, "event", None) == "firecrawl_api_call_failed"]
    assert failures
    assert all("fc-test" not in str(record.__dict__) for record in caplog.records)


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def test_fetch_truncates_strips_noise_and_replaces_inline_images() -> None:
    body = "x" * 50 + "![logo](data:image/png;base64,AAAA)"
    metadata = {"title": "T", "rawHtml": "<html>", "links": ["a"], "language": "en"}
    client, _ = _client(
        lambda endpoint, request: _Response({"success": True, "data": {"markdown": body, "metadata": metadata}})
    )

    [item] = client.fetch(["https://a.com"], "markdown", 40)
    assert item.title == "T"
    assert item.metadata == {"title": "T", "language": "en"}
    assert item.truncated is True and len(item.content) == 40

    [whole] = client.fetch(["https://a.com"], "markdown", 1000)
    assert whole.content.endswith("[IMAGE: logo]") and whole.truncated is False


def test_a_page_that_is_not_json_fails_alone() -> None:
    """网关返回 HTML 错误页时只算这一页失败，其余页照常返回、顺序与输入一致。"""

    def respond(endpoint: str, body: dict[str, Any]) -> _Response:
        if body["url"] == "https://bad.com":
            return _Response(None, raw="<html>gateway</html>")
        return _scrape_ok(endpoint, body)

    client, _ = _client(respond)
    items = client.fetch(["https://a.com", "https://bad.com", "https://c.com"], "markdown", 100)

    assert [item.url for item in items] == ["https://a.com", "https://bad.com", "https://c.com"]
    assert items[1].error.startswith("Firecrawl scrape failed")
    assert not items[0].error and not items[2].error


def test_missing_data_object_is_a_page_error() -> None:
    client, _ = _client(lambda endpoint, body: _Response({"success": True}))
    [item] = client.fetch(["https://a.com"], "markdown", 100)
    assert item.error == "Firecrawl scrape response missing 'data' object."


def test_not_configured_is_raised_not_swallowed_per_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Settings, "FIRECRAWL_API_KEY", "")
    client, _ = _client(_scrape_ok)
    with pytest.raises(FirecrawlNotConfiguredError):
        client.fetch(["https://a.com", "https://b.com"], "markdown", 100)


def test_fetch_tool_dedups_and_renders_each_page_in_its_own_block() -> None:
    client, calls = _client(_scrape_ok)

    observation = WebFetchTool(client).execute(
        urls=["example.com/a", "https://example.com/a", "https://b.com"], format="html", char_limit=2000
    )

    assert [call["body"] for call in calls] == [
        {"url": "https://example.com/a", "formats": ["html"]},
        {"url": "https://b.com", "formats": ["html"]},
    ]
    assert observation.status == "success"
    assert observation.content.count("<p>https://") == 2
    assert observation.display_data == {
        "kind": "web-fetch-urls",
        "partial": False,
        "urls": [{"url": "https://example.com/a"}, {"url": "https://b.com"}],
    }


def test_fetch_tool_partial_and_total_failure() -> None:
    def partial(urls: list[str], fmt: str, limit: int) -> list[WebFetchItem]:
        return [WebFetchItem(url=urls[0], content="ok"), WebFetchItem(url=urls[1], error="boom")]

    observation = WebFetchTool(SimpleNamespace(fetch=partial)).execute(  # type: ignore[arg-type]
        urls=["https://a.com", "https://b.com"]
    )
    assert observation.status == "success"
    assert observation.content.startswith("partial: 1 of 2 pages failed")
    assert "failed: https://b.com: boom" in observation.content
    assert observation.display_data["partial"] is True

    def all_down(urls: list[str], fmt: str, limit: int) -> list[WebFetchItem]:
        return [WebFetchItem(url=url, error="down") for url in urls]

    failed = WebFetchTool(SimpleNamespace(fetch=all_down)).execute(urls=["https://a.com"])  # type: ignore[arg-type]
    assert failed.status == "error"
    assert "Do not retry the same URLs" in failed.content


def test_fetch_tool_blocks_before_any_request() -> None:
    client, calls = _client(_scrape_ok)
    observation = WebFetchTool(client).execute(urls=["https://a.com", "http://localhost:3000"])
    assert calls == []
    assert observation.status == "error"
    assert observation.content.startswith("Blocked: the URL points to this machine or a private network.")


# ---------------------------------------------------------------------------
# URL 检查（不联网）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3000",
        "http://app.localhost",
        "http://127.0.0.1",
        "http://127.1",
        "http://0x7f.0.0.1",
        "http://2130706433",
        "http://[::1]",
        "http://10.0.0.1",
        "http://169.254.169.254",
        "http://224.0.0.1",
        "ftp://example.com",
        "https://user:pw@example.com",
        "https://example.com/?token=1",
        "https://example.com/?q=sk-abcdefghijk",
        "https://",
    ],
)
def test_urls_that_are_blocked(url: str) -> None:
    assert url_block_reason(normalize_url(url)) is not None


@pytest.mark.parametrize(
    "url", ["example.com/docs", "https://docs.python.org/3/", "http://8.8.8.8", "http://face.cafe"]
)
def test_public_urls_pass_without_dns(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_dns(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("URL check must not resolve hostnames")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    assert url_block_reason(normalize_url(url)) is None


def test_normalize_adds_https_when_scheme_missing() -> None:
    assert normalize_url("  example.com/a ") == "https://example.com/a"
    assert normalize_url("http://x.com") == "http://x.com"


# ---------------------------------------------------------------------------
# 参数模型与装配
# ---------------------------------------------------------------------------


def test_argument_models_enforce_their_limits() -> None:
    with pytest.raises(ValidationError):
        WebFetchArgs(urls=[f"https://a{i}.com" for i in range(Constant.Web.FETCH_URL_LIMIT_MAX + 1)])
    with pytest.raises(ValidationError):
        WebFetchArgs(urls=[])
    with pytest.raises(ValidationError):
        WebSearchArgs(query="q", limit=Constant.Web.SEARCH_LIMIT_MAX + 1)
    with pytest.raises(ValidationError):
        WebSearchArgs(query="q", limit="3")  # type: ignore[arg-type]


def test_definitions_are_always_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配置也注册：工具在集合里，调用时才告诉 Agent 怎么配置。"""

    monkeypatch.setattr(Settings, "FIRECRAWL_API_KEY", "")
    assert isinstance(build_web_search_definition(), ToolDefinition)
    assert isinstance(build_web_fetch_definition(), ToolDefinition)
