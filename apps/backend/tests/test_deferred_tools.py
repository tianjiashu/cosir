"""延迟工具核心：检索语义、``tool_search`` 结果格式、请求工具集推导、准入与执行、目录通知。

来源用替身（MCP 接入在第三阶段）；行为对照本仓对齐的 ToolSearch 契约。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.context.context_entry import ContextEntry
from app.core.context.prompt_sources.source import ItemDiff, SourceContext
from app.core.context.prompt_sources.sources.deferred_tools import DeferredToolsSource
from app.core.tool import external_tools as port
from app.core.tool.execute import tool_executor as tool_executor_module
from app.core.tool.execute.tool_executor import ToolExecutor
from app.core.tool.external_tools import ExternalCatalogUnavailable
from app.core.tool.handler.deferred.catalog_search import search_catalog
from app.core.tool.handler.deferred.deferred_catalog import DeferredCatalog
from app.core.tool.handler.tool_search import _LOADED_NOTE as LOADED_NOTE
from app.core.tool.handler.tool_search import NO_MATCH, ToolSearchTool
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import TOOL_SEARCH, ToolCall, ToolExecutionContext
from app.core.tool.schemas.request_tools import RequestTools
from app.core.tool.tool_registry import ToolRegistry
from app.models.external_tool import ExternalToolDefinition
from app.models.message_metadata import ToolMessageMetadata, parse_message_metadata
from app.models.tool_result_facts import ToolResultFacts
from tests.test_runtime_message_context_manager import _ContextStore, _manager


def _tool(name: str, description: str = "", parameters: dict[str, object] | None = None) -> ExternalToolDefinition:
    return ExternalToolDefinition(
        name=name,
        description=description,
        parameters=parameters
        if parameters is not None
        else {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        reference={"id": name},
    )


SLACK_SEND = _tool("mcp__slack__send_message", "Send a message to a Slack channel")
SLACK_LIST = _tool("mcp__slack__list_channels", "List Slack channels")
GITHUB_ISSUE = _tool("mcp__github__create_issue", "Create an issue; can send notifications")
CATALOG = (GITHUB_ISSUE, SLACK_LIST, SLACK_SEND)


class _FakeSource:
    def __init__(self, catalog: tuple[ExternalToolDefinition, ...] = CATALOG) -> None:
        self.catalog_value = catalog
        self.unavailable: str | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def catalog(self, workspace_root: str) -> tuple[ExternalToolDefinition, ...]:
        if self.unavailable is not None:
            raise ExternalCatalogUnavailable(self.unavailable)
        return self.catalog_value

    def execute(self, definition, arguments, execution_context):
        self.calls.append((definition.name, arguments))
        return tool_success(definition.name, "sent", tool_call_id=execution_context.tool_call_id)


@pytest.fixture
def source() -> Any:
    fake = _FakeSource()
    port.set_external_tools(fake)
    yield fake
    port.set_external_tools(None)


@pytest.fixture(autouse=True)
def _no_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_executor_module, "emit", lambda _event: None)


def _context(tmp_path: Path, call_id: str = "c1") -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=1, workspace_id=1, workspace_root=tmp_path, step_id="s", run_id=1, tool_call_id=call_id
    )


# —— 检索 ——


def test_select_is_exact_ordered_deduplicated_and_drops_unknown_names() -> None:
    picked = search_catalog(
        CATALOG, "select:mcp__slack__send_message, ghost,mcp__github__create_issue,mcp__slack__send_message", 1
    )
    # 不受 max_results 限制，按写下的顺序；大小写敏感。
    assert [d.name for d in picked] == ["mcp__slack__send_message", "mcp__github__create_issue"]
    assert search_catalog(CATALOG, "select:MCP__SLACK__SEND_MESSAGE", 5) == []


def test_keywords_rank_name_hits_above_description_hits_and_skip_misses() -> None:
    ranked = search_catalog(CATALOG, "send", 5)
    # 名字里有 send 的排在只有描述里有的前面；毫不相关的不硬凑。
    assert [d.name for d in ranked] == ["mcp__slack__send_message", "mcp__github__create_issue"]
    assert search_catalog(CATALOG, "jupyter notebook", 5) == []
    assert len(search_catalog(CATALOG, "slack", 1)) == 1


def test_required_term_filters_by_name_then_ranks_the_rest() -> None:
    assert [d.name for d in search_catalog(CATALOG, "+slack send", 5)] == ["mcp__slack__send_message"]
    assert [d.name for d in search_catalog(CATALOG, "+slack", 5)] == [
        "mcp__slack__list_channels",
        "mcp__slack__send_message",
    ]


# —— tool_search ——


def test_tool_search_returns_functions_block_and_records_loaded_tools(source, tmp_path) -> None:
    observation = ToolSearchTool().execute(
        query="select:mcp__slack__send_message", max_results=5, execution_context=_context(tmp_path)
    )
    assert observation.status == "success"
    line = json.dumps(DeferredCatalog._external_function(SLACK_SEND), ensure_ascii=False)
    assert observation.content == f"<functions>\n<function>{line}</function>\n</functions>\n{LOADED_NOTE}"
    assert list(json.loads(line)) == ["description", "name", "parameters"]
    assert observation.facts == ToolResultFacts(loaded_external_tools=(SLACK_SEND,))


def test_reloading_an_external_tool_returns_it_only_when_its_definition_changed(source, tmp_path) -> None:
    def search(loaded: tuple[ExternalToolDefinition, ...]) -> Any:
        context = replace(
            _context(tmp_path),
            request_tools=RequestTools.derive((), frozenset(), loaded_external=loaded),
        )
        return ToolSearchTool().execute(
            query="select:mcp__slack__send_message", max_results=5, execution_context=context
        )

    unchanged = search((SLACK_SEND,))
    assert "<functions>" not in unchanged.content and "Already loaded" in unchanged.content
    assert unchanged.facts is None
    # 服务改了定义（目录通知让模型重新加载）：照常给新版本、记一次加载。
    changed = search((_tool(SLACK_SEND.name, "old description"),))
    assert changed.content.startswith("<functions>")
    assert changed.facts == ToolResultFacts(loaded_external_tools=(SLACK_SEND,))


def test_tool_search_without_match_is_success_not_error(source, tmp_path) -> None:
    observation = ToolSearchTool().execute(query="jupyter", max_results=5, execution_context=_context(tmp_path))
    assert observation.status == "success"
    assert observation.content == NO_MATCH
    assert observation.facts is None


def test_tool_search_reports_unreadable_catalog(source, tmp_path) -> None:
    source.unavailable = "catalog is unavailable"
    observation = ToolSearchTool().execute(query="slack", max_results=5, execution_context=_context(tmp_path))
    assert observation.status == "error"
    assert "catalog is unavailable" in observation.content


# —— 加载事实与请求工具集 ——


def test_loaded_tools_survive_persistence_and_metadata_merges() -> None:
    metadata = ToolMessageMetadata(status="completed", facts=ToolResultFacts(loaded_external_tools=(SLACK_SEND,)))
    assert parse_message_metadata("tool", json.loads(metadata.model_dump_json())) == metadata
    # 后来的更新（如 Run 外的结算）不带它时保留：否则已加载的工具会被静默卸掉。
    assert ToolMessageMetadata(status="cancelled").merged_onto(metadata).facts.loaded_external_tools == (SLACK_SEND,)


def test_context_lists_loaded_tools_in_order_including_inherited_rows() -> None:
    manager = _manager(_ContextStore(max_sequence=3))
    manager._entries = [
        ContextEntry(
            {"role": "tool", "tool_call_id": "a", "content": "x"},
            None,  # 分叉继承的行
            1,
            metadata=ToolMessageMetadata(
                status="completed", facts=ToolResultFacts(loaded_external_tools=(SLACK_SEND,))
            ),
            turn=1,
        ),
        ContextEntry(
            {"role": "tool", "tool_call_id": "b", "content": "y"},
            2,
            2,
            metadata=ToolMessageMetadata(status="completed"),
            turn=2,
        ),
        ContextEntry(
            {"role": "tool", "tool_call_id": "c", "content": "z"},
            2,
            3,
            metadata=ToolMessageMetadata(
                status="completed", facts=ToolResultFacts(loaded_external_tools=(GITHUB_ISSUE,))
            ),
            turn=2,
        ),
    ]
    assert manager.loaded_external_tools() == [SLACK_SEND, GITHUB_ISSUE]


def test_loaded_external_tools_are_admitted_without_touching_the_tool_list() -> None:
    base = ({"name": "read_file", "strict": True, "parameters": {}}, {"name": TOOL_SEARCH, "parameters": {}})
    newer = _tool(SLACK_SEND.name, "v2")
    tools = RequestTools.derive(
        base,
        frozenset(),
        loaded_external=[SLACK_SEND, GITHUB_ISSUE, newer, _tool("read_file", "shadow")],
    )
    # 工具列表不变（请求前缀不变、缓存不断）；定义取最后一次加载，与基础工具同名的不覆盖。
    assert tools.schemas == base
    assert tools.external[SLACK_SEND.name] == newer
    assert tools.admitted == {"read_file", TOOL_SEARCH, SLACK_SEND.name, GITHUB_ISSUE.name}
    assert set(tools.external) == {SLACK_SEND.name, GITHUB_ISSUE.name}
    banned = RequestTools.derive(base, frozenset({GITHUB_ISSUE.name}), loaded_external=[GITHUB_ISSUE])
    assert GITHUB_ISSUE.name not in banned.admitted and not banned.external


# —— 准入与执行 ——


def _execute(tmp_path: Path, name: str, arguments: str, loaded=(SLACK_SEND,), base=(TOOL_SEARCH,)):
    tools = RequestTools.derive(tuple({"name": n} for n in base), frozenset(), loaded_external=list(loaded))
    return ToolExecutor(ToolRegistry([])).execute(
        ToolCall(tool_name=name, arguments=arguments, call_id="c1"), _context(tmp_path), tools
    )


def test_loaded_deferred_call_goes_to_source_with_whole_arguments(source, tmp_path) -> None:
    observation = _execute(tmp_path, SLACK_SEND.name, '{"text": "hi"}')
    assert observation.status == "success" and observation.content == "sent"
    assert source.calls == [(SLACK_SEND.name, {"text": "hi"})]


def test_deferred_arguments_are_checked_against_the_source_schema(source, tmp_path) -> None:
    observation = _execute(tmp_path, SLACK_SEND.name, '{"text": 3}')
    assert observation.status == "error" and "invalid tool arguments" in observation.content
    assert source.calls == []


def test_an_external_intent_parameter_is_an_ordinary_argument(source, tmp_path) -> None:
    owns = _tool("mcp__x__classify", parameters={"type": "object", "properties": {"intent": {"type": "string"}}})
    assert DeferredCatalog._external_function(owns)["parameters"] == owns.parameters
    observation = _execute(tmp_path, owns.name, '{"intent": "refund"}', loaded=(owns,))
    assert observation.status == "success"
    assert source.calls == [(owns.name, {"intent": "refund"})]


def test_unloaded_catalog_tool_is_told_to_load_first(source, tmp_path) -> None:
    observation = _execute(tmp_path, GITHUB_ISSUE.name, '{"text": "x"}')
    assert observation.status == "error"
    assert f'"select:{GITHUB_ISSUE.name}"' in observation.content
    assert source.calls == []


def test_without_tool_search_an_unknown_name_is_just_unknown(tmp_path) -> None:
    # 来源都没装配：没有检索工具的 Session 不会去问它。
    observation = _execute(tmp_path, GITHUB_ISSUE.name, "{}", loaded=(), base=("read_file",))
    assert observation.status == "error" and observation.content.startswith("unknown tool")


def test_source_exception_becomes_error_observation(source, tmp_path, monkeypatch) -> None:
    def boom(*_args: Any) -> Any:
        raise RuntimeError("server crashed")

    monkeypatch.setattr(source, "execute", boom)
    observation = _execute(tmp_path, SLACK_SEND.name, '{"text": "hi"}')
    assert observation.status == "error" and "server crashed" in observation.content


# —— 目录通知 ——


def _items(source: _FakeSource) -> dict[str, Any]:
    context = SourceContext("/w", frozenset({TOOL_SEARCH}), SimpleNamespace(), None)  # type: ignore[arg-type]
    return DeferredToolsSource().project(context)


def test_catalog_renders_names_with_load_instruction(source) -> None:
    text = DeferredToolsSource().render(_items(source))
    assert text.startswith("The following deferred tools are now available via tool_search.")
    # 目录是背景信息：结尾提醒模型不要主动讲给用户听。
    assert text.endswith("unless they ask or it is directly relevant to their request.")
    assert "\n".join(d.name for d in CATALOG) + "\n\nThis is ambient context" in text


def test_catalog_changes_use_available_changed_and_removed_wording(source) -> None:
    before = {k: SimpleNamespace(fingerprint=v.fingerprint, text=v.text) for k, v in _items(source).items()}
    source.catalog_value = (_tool(GITHUB_ISSUE.name, "new description"), SLACK_SEND, _tool("mcp__db__query"))
    items = _items(source)
    diff = ItemDiff.between(before, items)  # type: ignore[arg-type]
    text = DeferredToolsSource().describe(diff, items, before)  # type: ignore[arg-type]
    assert "now available via tool_search" in text and "mcp__db__query" in text
    assert "changed their definitions" in text and GITHUB_ISSUE.name in text
    assert "no longer available. Do not search for them" in text and SLACK_LIST.name in text
    assert text.endswith("unless they ask or it is directly relevant to their request.")


def test_unreadable_catalog_projects_one_unavailable_item(source) -> None:
    source.unavailable = "catalog is unavailable"
    assert DeferredToolsSource().render(_items(source)).startswith("catalog is unavailable\n\nThis is ambient context")
