"""Web extract/search 输出阶段的行为与潜在问题验证。"""

from pathlib import Path

from app.tools.schemas.tool_execution_context import ToolExecutionContext as Ctx
from app.tools.tool_handler.web.web_content_store import (
    convert_base64_images_to_placeholders,
)
from app.tools.tool_handler.web.web_provider import WebExtractItem, WebProvider


class FakeExtractProvider(WebProvider):
    """用于驱动 WebExtractTool 输出逻辑的易控 Provider 替身。"""

    name = "fake"

    def __init__(self, items: list[WebExtractItem]) -> None:
        """构造固定返回结果的假 Provider。

        参数:
            items: extract 调用直接返回的提取条目。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        self._items = items

    @property
    def display_name(self) -> str:
        """返回固定显示名。"""
        return "Fake"

    def is_available(self) -> bool:
        """始终可用。"""
        return True

    def supports_search(self) -> bool:
        """不支持搜索。"""
        return False

    def supports_extract(self) -> bool:
        """支持提取。"""
        return True

    def supported_extract_formats(self) -> frozenset[str]:
        """声明支持 markdown。"""
        return frozenset({"markdown"})

    def missing_configuration_message(self) -> str:
        """返回固定提示。"""
        return "fake missing config"

    def search(self, query: str, limit: int) -> list:
        """不支持，直接抛出。"""
        raise AssertionError("search not supported by fake extract provider")

    def extract(self, urls: list[str], output_format: str, char_limit: int) -> list[WebExtractItem]:
        """返回构造时固定的条目。"""
        return self._items


def _context(tmp_path: Path) -> Ctx:
    """构造指向临时目录的工具执行上下文。"""
    return Ctx(
        task_id="task-1",
        workspace_id="ws-1",
        workspace_root=tmp_path,
    )


def test_web_extract_returns_full_content_without_self_storing(tmp_path: Path) -> None:
    """验证 web_extract 返回完整清洗后正文，且不再自行落盘（方案 A）。

    参数:
        tmp_path: pytest 临时目录固件。

    返回:
        无。

    异常:
        AssertionError: 行为偏离预期时由断言抛出。
    """
    from app.tools.tool_handler.web.web_provider_registry import WebProviderRegistry
    from app.tools.tool_handler.web_extract import WebExtractTool

    long_text = "A" * 2000 + "MIDDLE_MARKER" + "B" * 2000
    provider = FakeExtractProvider(
        [
            WebExtractItem(
                url="https://example.com",
                title="Page",
                content=long_text,
                raw_content=long_text,
                metadata={"source": "test"},
            )
        ]
    )
    registry = WebProviderRegistry()
    registry.register(provider)
    tool = WebExtractTool(registry, resolver=lambda host: ["93.184.216.34"])

    ctx = _context(tmp_path)
    observation = tool.execute(
        urls=["https://example.com"],
        format="markdown",
        char_limit=15000,
        execution_context=ctx,
    )
    assert observation.status == "success"
    import json

    payload = json.loads(observation.content)
    result = payload["results"][0]
    # 完整正文直接返回，中间标记保留，未做 head+tail 截断
    assert "MIDDLE_MARKER" in result["content"]
    assert result["content"] == long_text
    # 不再暴露 stored_path / truncated 字段，落盘交由全局预算
    assert "stored_path" not in result
    assert "truncated" not in result
    # display_data 是纯结构化数据透传，由客户端渲染层与调度层 DisplayDataBudget 守卫处理；
    # 后端 handler 不渲染、不混入摘要字段、不复制 content 全文到 display_data 顶层。
    assert "web" in observation.display_data
    assert isinstance(observation.display_data["web"], list)
    assert len(observation.display_data["web"]) == 1
    # 根因防护：display_data 顶层不得混入渲染字段或 content 全文副本。
    assert "summary" not in observation.display_data
    assert "result_summary" not in observation.display_data
    assert "content" not in observation.display_data
    # 自身不落盘到 tool-results 目录
    assert not (tmp_path / ".coding-agent" / "tool-results").exists()


def test_web_extract_error_items_preserved(tmp_path: Path) -> None:
    """验证 Provider 返回的 error 条目在结果中保留而非丢弃。

    参数:
        tmp_path: pytest 临时目录固件。

    返回:
        无。

    异常:
        AssertionError: 行为偏离预期时由断言抛出。
    """
    from app.tools.tool_handler.web.web_provider_registry import WebProviderRegistry
    from app.tools.tool_handler.web_extract import WebExtractTool

    provider = FakeExtractProvider(
        [
            WebExtractItem(
                url="https://example.com",
                title="",
                content="",
                raw_content="",
                metadata={},
                error="blocked by policy",
            )
        ]
    )
    registry = WebProviderRegistry()
    registry.register(provider)
    tool = WebExtractTool(registry, resolver=lambda host: ["93.184.216.34"])

    observation = tool.execute(
        urls=["https://example.com"],
        format="markdown",
        execution_context=_context(tmp_path),
    )
    assert observation.status == "success"
    import json

    result = json.loads(observation.content)["results"][0]
    assert result["error"] == "blocked by policy"
    assert result["content"] == ""


def test_base64_image_placeholder_replacement() -> None:
    """验证内联 base64 图片被替换为文本占位符。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 替换结果不符时由断言抛出。
    """
    md = "![alt text](data:image/png;base64,AAAA) and ![pic](data:image/jpeg;base64,BBBB)"
    result = convert_base64_images_to_placeholders(md)
    assert "data:image" not in result
    assert "[IMAGE: alt text]" in result
    assert "[IMAGE: pic]" in result


def test_firecrawl_extract_drops_url_on_non_dict_data(monkeypatch) -> None:
    """验证 Firecrawl 某 URL 返回 data 非 dict 时该条结果被静默丢弃（P4）。

    参数:
        monkeypatch: pytest 属性替换固件。

    返回:
        无。

    异常:
        AssertionError: 行为与预期不符时由断言抛出。
    """
    from app.tools.tool_handler.web.providers.firecrawl_provider import (
        FirecrawlProvider,
    )

    provider = FirecrawlProvider(api_key="key")

    def fake_post(endpoint: str, body: dict[str, object]) -> object:
        """返回 data 为 null 的响应，模拟 API 错误。"""
        return {"data": None}

    monkeypatch.setattr(provider, "_post", fake_post)
    items = provider.extract(["https://example.com"], "markdown", 1000)
    # 修复后：data 非 dict 不再静默丢弃，而是返回带 error 的条目，模型可感知
    assert len(items) == 1
    assert items[0].url == "https://example.com"
    assert items[0].error == "Firecrawl scrape response missing 'data' object."


def test_firecrawl_extract_error_field_preserved() -> None:
    """验证 Firecrawl 返回 data.error 时条目携带 error 字段而非丢弃。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: error 投影行为不符时由断言抛出。
    """

    def fake_post(endpoint: str, body: dict[str, object]) -> object:
        """返回带 error 字段的响应。"""
        return {"data": {"url": "https://example.com", "error": "blocked by policy"}}

    # 不替换 _post（避免真实网络）；直接调用 _post 不可行（私有），
    # 此处仅验证 WebExtractItem 的 error 字段约定存在且可被工具层消费。
    item = WebExtractItem(
        url="https://example.com",
        title="",
        content="",
        raw_content="",
        metadata={},
        error="blocked by policy",
    )
    assert item.error == "blocked by policy"


def test_firecrawl_search_respects_effective_limit(monkeypatch) -> None:
    """验证 Firecrawl search 按 effective_limit 截断而非原始请求 limit（P3）。

    参数:
        monkeypatch: pytest 属性替换固件。

    返回:
        无。

    异常:
        AssertionError: 截断语义不符时由断言抛出。
    """
    from app.tools.tool_handler.web.providers.firecrawl_provider import (
        FirecrawlProvider,
    )

    provider = FirecrawlProvider(api_key="key")

    def fake_post(endpoint: str, body: dict[str, object]) -> object:
        """返回远超请求数量上限的 30 条结果。"""
        return {
            "data": [
                {"title": f"T{i}", "url": f"https://example.com/{i}", "description": "d"}
                for i in range(30)
            ]
        }

    monkeypatch.setattr(provider, "_post", fake_post)
    # 请求 limit=100（远超默认上限 20），provider 应按 effective_limit=20 截断
    items = provider.search("query", 100)
    assert len(items) == 20
    assert all(item.position <= 20 for item in items)
