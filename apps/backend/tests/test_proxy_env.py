"""代理配置 ``app.utils.proxy`` 与 web/model client 注入的单元测试。

不访问真实操作系统代理：``urllib.request.getproxies`` 与 ``resolve_httpx_proxy``
均通过 monkeypatch 控制返回值。重点验证：
- ``resolve_httpx_proxy`` 的开关解析与系统代理回退规则；
- FirecrawlProvider 复用同一个带代理的 httpx.Client（search / extract 共享）。
"""

from __future__ import annotations

from collections.abc import Mapping
from unittest.mock import patch

import pytest

from app.core.tools.tool_handler.web.providers.firecrawl_provider import FirecrawlProvider
from app.utils import http_proxy as proxy_module
from app.utils.http_proxy import resolve_httpx_proxy


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例前清理代理相关环境变量，避免本机配置干扰测试结果。"""

    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "CODING_AGENT_PROXY_AUTO_DETECT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_system_proxy() -> Mapping[str, str]:
    """返回一个稳定的系统代理配置，供 mock 使用。"""

    return {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}


def test_resolves_https_proxy_when_enabled(fake_system_proxy: Mapping[str, str]) -> None:
    """默认开启且系统存在 https 代理时，应返回该代理 URL。"""

    with patch("urllib.request.getproxies", return_value=dict(fake_system_proxy)):
        proxy = resolve_httpx_proxy()

    assert proxy == "http://127.0.0.1:7897"


def test_falls_back_to_http_proxy(fake_system_proxy: Mapping[str, str]) -> None:
    """仅有 http 代理时，应回退使用 http 代理 URL。"""

    system = {"http": "http://127.0.0.1:7897"}
    with patch("urllib.request.getproxies", return_value=system):
        proxy = resolve_httpx_proxy()

    assert proxy == "http://127.0.0.1:7897"


def test_returns_none_without_system_proxy(
    fake_system_proxy: Mapping[str, str],
) -> None:
    """系统无代理时（或读取失败）应返回 None，表示不走代理。"""

    with patch("urllib.request.getproxies", return_value={}):
        assert resolve_httpx_proxy() is None

    with patch("urllib.request.getproxies", side_effect=RuntimeError("boom")):
        assert resolve_httpx_proxy() is None


def test_disabled_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CODING_AGENT_PROXY_AUTO_DETECT=false`` 时应跳过检测返回 None，且不读取系统代理。"""

    monkeypatch.setenv("CODING_AGENT_PROXY_AUTO_DETECT", "false")
    fake = {"http": "http://127.0.0.1:7897"}
    with patch("urllib.request.getproxies", return_value=fake) as mock_get:
        assert resolve_httpx_proxy() is None

    mock_get.assert_not_called()


class _CaptureClient:
    """记录构造参数并模拟最小 httpx.Client 行为，用于断言代理注入与复用。"""

    instances: list[_CaptureClient] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.kwargs = dict(kwargs)
        self.closed = False
        _CaptureClient.instances.append(self)

    def close(self) -> None:
        self.closed = True

    def post(self, url: str, json: object = None, headers: object = None) -> _FakeResponse:
        return _FakeResponse()


class _FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return {"success": True, "data": {}}


def test_firecrawl_reuses_proxy_aware_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search / extract 应复用同一个带代理的 httpx.Client，且 proxy 由配置注入。"""

    _CaptureClient.instances.clear()
    monkeypatch.setattr(proxy_module.httpx, "Client", _CaptureClient)
    monkeypatch.setattr(
        proxy_module, "resolve_httpx_proxy", lambda: "http://127.0.0.1:7890"
    )

    provider = FirecrawlProvider(api_key="dummy")
    provider._post("search", {"query": "q"})
    provider._post("scrape", {"url": "https://example.com"})

    assert len(_CaptureClient.instances) == 1, "应复用同一 client 而非每次新建"
    assert _CaptureClient.instances[0].kwargs.get("proxy") == "http://127.0.0.1:7890"


def test_firecrawl_client_proxy_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动检测关闭时，注入的 proxy 应为 None（等价不设置代理）。"""

    _CaptureClient.instances.clear()
    monkeypatch.setattr(proxy_module.httpx, "Client", _CaptureClient)
    monkeypatch.setattr(proxy_module, "resolve_httpx_proxy", lambda: None)

    provider = FirecrawlProvider(api_key="dummy")
    provider._post("search", {"query": "q"})

    assert _CaptureClient.instances[0].kwargs.get("proxy") is None


def test_proxy_client_rebuilds_when_proxy_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """系统代理变化时，ProxyHttpClient 应关闭旧 client 并重建新 client（进程不重启即实时生效）。"""

    from app.utils.http_proxy import ProxyHttpClient

    _CaptureClient.instances.clear()
    monkeypatch.setattr(proxy_module.httpx, "Client", _CaptureClient)
    states = iter(["http://127.0.0.1:7897", None, "http://127.0.0.1:7890"])
    monkeypatch.setattr(proxy_module, "resolve_httpx_proxy", lambda: next(states))

    holder = ProxyHttpClient()
    holder.get(timeout=10)
    holder.get(timeout=10)  # 代理变为 None -> 重建
    holder.get(timeout=10)  # 代理变为 7890 -> 重建

    assert len(_CaptureClient.instances) == 3, "代理每次变化都应重建 client"
    assert _CaptureClient.instances[0].closed is True, "旧 client 应被关闭"
    assert _CaptureClient.instances[1].closed is True
    assert _CaptureClient.instances[2].closed is False
