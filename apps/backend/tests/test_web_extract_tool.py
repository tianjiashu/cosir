"""web_extract 工具的单元测试。"""

import json
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.tools.schemas.tool_execution_context import ToolExecutionContext
from app.tools.tool_handler.web.web_extract import WebExtractTool
from app.tools.tool_handler.web.web_provider import WebExtractItem
from app.tools.tool_handler.web.web_provider_registry import WebProviderRegistry


class FakeExtractProvider:
    """返回固定正文的无网络提取 Provider。"""

    name = "fake"
    display_name = "Fake"

    def __init__(self, content: str = "Hello world") -> None:
        """初始化固定内容和调用记录。

        参数:
            content: 每个提取结果返回的正文内容。

        返回:
            无。

        异常:
            无。

        副作用:
            创建调用记录列表。
        """

        self.content = content
        self.calls: list[tuple[list[str], str, int]] = []

    def is_available(self) -> bool:
        """返回 Provider 可用状态。

        参数:
            无。

        返回:
            始终返回 ``True``。

        异常:
            无。

        副作用:
            无。
        """

        return True

    def supports_search(self) -> bool:
        """返回搜索能力状态。

        参数:
            无。

        返回:
            始终返回 ``False``。

        异常:
            无。

        副作用:
            无。
        """

        return False

    def supports_extract(self) -> bool:
        """返回正文提取能力状态。

        参数:
            无。

        返回:
            始终返回 ``True``。

        异常:
            无。

        副作用:
            无。
        """

        return True

    def missing_configuration_message(self) -> str:
        """返回缺少配置时的诊断文本。

        参数:
            无。

        返回:
            固定英文诊断文本。

        异常:
            无。

        副作用:
            无。
        """

        return "Fake is not configured"

    def extract(
        self,
        urls: list[str],
        output_format: str,
        char_limit: int,
    ) -> list[WebExtractItem]:
        """记录调用并返回固定提取结果。

        参数:
            urls: 已通过工具安全校验的 URL 列表。
            output_format: 调用方请求的网页正文格式。
            char_limit: Provider 接收的每页字符上限。

        返回:
            与首个 URL 对应的固定网页正文。

        异常:
            无。

        副作用:
            向 ``calls`` 追加一次调用记录。
        """

        self.calls.append((urls, output_format, char_limit))
        return [
            WebExtractItem(
                url=urls[0],
                title="Page",
                content=self.content,
                raw_content=self.content,
                metadata={"source": "fake"},
            )
        ]


class FakeSearchOnlyProvider(FakeExtractProvider):
    """仅声明搜索能力的已配置 Provider。"""

    name = "search-only"
    display_name = "Search Only"

    def supports_search(self) -> bool:
        """返回搜索能力状态。

        参数:
            无。

        返回:
            始终返回 ``True``。

        异常:
            无。

        副作用:
            无。
        """

        return True

    def supports_extract(self) -> bool:
        """返回正文提取能力状态。

        参数:
            无。

        返回:
            始终返回 ``False``。

        异常:
            无。

        副作用:
            无。
        """

        return False


class FakeAsyncExtractProvider(FakeExtractProvider):
    """以协程方式返回固定正文的无网络提取 Provider。"""

    async def extract(
        self,
        urls: list[str],
        output_format: str,
        char_limit: int,
    ) -> list[WebExtractItem]:
        """记录调用并通过协程返回固定提取结果。

        参数:
            urls: 已通过工具安全校验的 URL 列表。
            output_format: 调用方请求的网页正文格式。
            char_limit: Provider 接收的每页字符上限。

        返回:
            与父类同步 Provider 一致的固定网页正文。

        异常:
            无。

        副作用:
            向 ``calls`` 追加一次调用记录，但不发起真实网络请求。
        """

        return super().extract(urls, output_format, char_limit)


class FakeFailedExtractProvider(FakeExtractProvider):
    """返回单页提取错误的无网络 Provider。"""

    def extract(
        self,
        urls: list[str],
        output_format: str,
        char_limit: int,
    ) -> list[WebExtractItem]:
        """记录调用并返回不含正文的单页 Provider 错误。

        参数:
            urls: 已通过工具安全校验的 URL 列表。
            output_format: 调用方请求的网页正文格式。
            char_limit: Provider 接收的每页字符上限。

        返回:
            含稳定错误文本的单页提取结果。

        异常:
            无。

        副作用:
            向 ``calls`` 追加一次调用记录，但不发起真实网络请求。
        """

        self.calls.append((urls, output_format, char_limit))
        return [
            WebExtractItem(
                url=urls[0],
                title="Page",
                content="",
                raw_content="",
                metadata={"source": "fake"},
                error="Page extraction failed.",
            )
        ]


def build_tool(provider: FakeExtractProvider) -> WebExtractTool:
    """使用无网络 DNS 解析器构造待测工具。

    参数:
        provider: 注册到测试 Provider 注册表的实例。

    返回:
        绑定测试 Provider 和固定公网地址解析器的工具实例。

    异常:
        无。

    副作用:
        创建并填充新的 Provider 注册表。
    """

    registry = WebProviderRegistry()
    registry.register(provider)
    return WebExtractTool(provider_registry=registry, resolver=lambda _host: ["93.184.216.34"])


def test_web_extract_accepts_search_result_objects(tmp_path: Path) -> None:
    """验证 web_extract 接受含 URL 的搜索结果对象。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。

    返回:
        无。

    异常:
        AssertionError: 当返回内容、URL 规范化或 Provider 调用参数不符合预期时抛出。

    副作用:
        通过测试 Provider 记录一次提取调用，但不发起真实网络请求。
    """

    provider = FakeExtractProvider()
    tool = build_tool(provider)
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute([{"url": "example.com/page"}], execution_context=context)
    payload = json.loads(observation.content)

    assert observation.status == "success"
    assert payload["success"] is True
    assert payload["results"][0]["url"] == "https://example.com/page"
    assert payload["results"][0]["content"] == "Hello world"
    assert provider.calls == [
        (["https://example.com/page"], "markdown", Settings.WEB_EXTRACT_CHAR_LIMIT)
    ]


def test_web_extract_accepts_search_result_href(tmp_path: Path) -> None:
    """验证 web_extract 接受含 href 的搜索结果对象。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。

    返回:
        无。

    异常:
        AssertionError: 当 URL 规范化或 Provider 调用参数不符合预期时抛出。

    副作用:
        通过测试 Provider 记录一次提取调用，但不发起真实网络请求。
    """

    provider = FakeExtractProvider()
    tool = build_tool(provider)
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute([{"href": "example.com/page"}], execution_context=context)

    assert observation.status == "success"
    assert provider.calls == [
        (["https://example.com/page"], "markdown", Settings.WEB_EXTRACT_CHAR_LIMIT)
    ]


def test_web_extract_blocks_secret_urls_before_provider_io(tmp_path: Path) -> None:
    """验证 web_extract 在 Provider 调用前拦截带密钥的 URL。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。

    返回:
        无。

    异常:
        AssertionError: 当错误信息或 Provider 调用记录不符合预期时抛出。

    副作用:
        不发起真实网络请求。
    """

    provider = FakeExtractProvider()
    tool = build_tool(provider)
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute(["https://example.com/?api_key=secret"], execution_context=context)

    assert observation.status == "error"
    assert "credential-like query parameter" in observation.error
    assert provider.calls == []


def test_web_extract_blocks_embedded_secrets_before_provider_io(tmp_path: Path) -> None:
    """验证 web_extract 在 Provider 调用前拦截嵌入 URL 的密钥。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。

    返回:
        无。

    异常:
        AssertionError: 当错误信息或 Provider 调用记录不符合预期时抛出。

    副作用:
        不发起真实网络请求。
    """

    provider = FakeExtractProvider()
    tool = build_tool(provider)
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute(["https://example.com/sk-abcdefgh"], execution_context=context)

    assert observation.status == "error"
    assert "embedded secret" in observation.error
    assert provider.calls == []


def test_web_extract_requires_execution_context() -> None:
    """验证 web_extract 拒绝缺少工作区边界的请求。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当缺少执行上下文未返回错误观察结果时抛出。

    副作用:
        不发起真实网络请求。
    """

    observation = build_tool(FakeExtractProvider()).execute(["https://example.com"])

    assert observation.status == "error"
    assert "execution context" in observation.error


@pytest.mark.parametrize(
    ("urls", "resolver", "message"),
    [
        (["file:///tmp/page"], lambda _host: ["93.184.216.34"], "scheme must be http or https"),
        (["http://localhost/page"], lambda _host: ["127.0.0.1"], "private or internal"),
        (["https://shared-address.example"], lambda _host: ["100.64.0.1"], "private or internal"),
    ],
)
def test_web_extract_blocks_unsafe_urls_before_provider_io(
    tmp_path: Path,
    urls: list[str],
    resolver: object,
    message: str,
) -> None:
    """验证 web_extract 在 Provider 调用前拒绝非公网 HTTP(S) URL。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。
        urls: 预期被拒绝的 URL 输入列表。
        resolver: 返回测试网络地址的注入 DNS 解析器。
        message: 预期出现在错误文本内的英文片段。

    返回:
        无。

    异常:
        AssertionError: 当错误信息或 Provider 调用记录不符合预期时抛出。

    副作用:
        不发起真实网络请求。
    """

    provider = FakeExtractProvider()
    registry = WebProviderRegistry()
    registry.register(provider)
    tool = WebExtractTool(provider_registry=registry, resolver=resolver)
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute(urls, execution_context=context)

    assert observation.status == "error"
    assert message in observation.error
    assert provider.calls == []


def test_web_extract_rejects_too_many_urls_before_provider_io(tmp_path: Path) -> None:
    """验证 web_extract 拒绝超过配置上限的 URL 输入。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。

    返回:
        无。

    异常:
        AssertionError: 当上限错误或 Provider 调用记录不符合预期时抛出。

    副作用:
        不发起真实网络请求。
    """

    provider = FakeExtractProvider()
    tool = build_tool(provider)
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)
    urls = [f"https://example{i}.com" for i in range(Settings.WEB_EXTRACT_URL_LIMIT_MAX + 1)]

    observation = tool.execute(urls, execution_context=context)

    assert observation.status == "error"
    assert f"at most {Settings.WEB_EXTRACT_URL_LIMIT_MAX} URLs" in observation.error
    assert provider.calls == []


def test_web_extract_reports_search_only_backend(tmp_path: Path) -> None:
    """验证显式搜索专用后端返回确定性提取错误。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。

    返回:
        无。

    异常:
        AssertionError: 当错误文本或 Provider 调用记录不符合预期时抛出。

    副作用:
        临时修改并恢复网页提取后端配置，不发起真实网络请求。
    """

    provider = FakeSearchOnlyProvider()
    registry = WebProviderRegistry()
    registry.register(provider)
    tool = WebExtractTool(provider_registry=registry, resolver=lambda _host: ["93.184.216.34"])
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    original_backend = Settings.WEB_EXTRACT_BACKEND
    try:
        Settings.override(WEB_EXTRACT_BACKEND=provider.name)
        observation = tool.execute(["https://example.com"], execution_context=context)
    finally:
        Settings.override(WEB_EXTRACT_BACKEND=original_backend)

    assert observation.status == "error"
    assert observation.error == (
        "Search Only is a search-only backend and cannot extract URL content. "
        "Configure an extract-capable backend such as firecrawl, tavily, exa, or parallel."
    )
    assert provider.calls == []


def test_web_extract_stores_full_clean_content_within_workspace(tmp_path: Path) -> None:
    """验证 web_extract 去除内联图片并在超限时保存完整内容。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。

    返回:
        无。

    异常:
        AssertionError: 当截断标记、工作区相对路径或清理后的存储内容不符合预期时抛出。

    副作用:
        在 pytest 的临时工作区内创建完整内容文件，不发起真实网络请求。
    """

    provider = FakeExtractProvider("Before ![diagram](data:image/png;base64,AAAA) after")
    tool = build_tool(provider)
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute(["https://example.com"], char_limit=10, execution_context=context)
    payload = json.loads(observation.content)
    result = payload["results"][0]

    assert result["truncated"] is True
    assert result["stored_path"].startswith(".coding-agent/tool-results/web/")
    assert "[IMAGE: diagram]" in (tmp_path / result["stored_path"]).read_text(encoding="utf-8")


def test_web_extract_runs_async_provider_without_network(tmp_path: Path) -> None:
    """验证 web_extract 能在私有事件循环中运行异步 Provider。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。

    返回:
        无。

    异常:
        AssertionError: 当异步 Provider 未成功执行或调用参数不符合预期时抛出。

    副作用:
        通过测试 Provider 记录一次协程提取调用，但不发起真实网络请求。
    """

    provider = FakeAsyncExtractProvider()
    tool = build_tool(provider)
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute(["https://example.com"], execution_context=context)

    assert observation.status == "success"
    assert provider.calls == [
        (["https://example.com"], "markdown", Settings.WEB_EXTRACT_CHAR_LIMIT)
    ]


@pytest.mark.parametrize("output_format", ["html", "text"])
def test_web_extract_passes_requested_format_to_provider(
    tmp_path: Path,
    output_format: str,
) -> None:
    """验证 web_extract 将模型请求的正文格式传递给 Provider。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。
        output_format: 期望透传到 Provider 的网页正文格式。

    返回:
        无。

    异常:
        AssertionError: Provider 未收到请求格式或工具执行失败时抛出。

    副作用:
        通过测试 Provider 记录一次提取调用，但不发起真实网络请求。
    """

    provider = FakeExtractProvider()
    tool = build_tool(provider)
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute(
        ["https://example.com"],
        format=output_format,
        execution_context=context,
    )

    assert observation.status == "success"
    assert provider.calls == [
        (["https://example.com"], output_format, Settings.WEB_EXTRACT_CHAR_LIMIT)
    ]


def test_web_extract_does_not_store_failed_page_content(tmp_path: Path) -> None:
    """验证 web_extract 不会为单页 Provider 错误写入工作区。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。

    返回:
        无。

    异常:
        AssertionError: 当 Provider 错误仍产生正文存储或结果字段不符合预期时抛出。

    副作用:
        通过测试 Provider 记录一次提取调用，但不发起真实网络请求或写入网页内容。
    """

    provider = FakeFailedExtractProvider()
    tool = build_tool(provider)
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = tool.execute(["https://example.com"], execution_context=context)
    payload = json.loads(observation.content)
    result = payload["results"][0]

    assert observation.status == "success"
    assert result["content"] == ""
    assert result["stored_path"] == ""
    assert result["truncated"] is False
    assert result["error"] == "Page extraction failed."
    assert not (tmp_path / ".coding-agent").exists()
