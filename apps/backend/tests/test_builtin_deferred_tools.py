"""内置工具延迟加载：工具面固化、请求工具集、准入、``tool_search`` 与目录，以及完整链路上的加载与调用。

外部工具（MCP）的同一套机制在 ``test_deferred_tools`` / ``test_mcp_deferred_e2e``；这里只测内置工具多出来的部分。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.config.configuration import get_tool_registry
from app.core.context.prompt_sources.source import SourceContext
from app.core.context.prompt_sources.sources.deferred_tools import DeferredToolsSource
from app.core.tool import external_tools as port
from app.core.tool.execute import tool_executor as tool_executor_module
from app.core.tool.execute.tool_executor import ToolExecutor
from app.core.tool.external_tools import ExternalCatalogUnavailable
from app.core.tool.handler.tool_search import _LOADED_NOTE as LOADED_NOTE
from app.core.tool.handler.tool_search import ToolSearchTool
from app.core.tool.multi_agent.session_tool_scope import (
    freeze_tool_surface,
    main_agent_tool_surface,
    tools_for_session,
)
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_BROWSER_NAVIGATE,
    TOOL_FORK_SESSION,
    TOOL_PROPOSE_AGENT_CONFIGURATION,
    TOOL_READ_FILE,
    TOOL_SEARCH,
    TOOL_SESSION_LIST,
    TOOL_SESSION_READ,
    TOOL_SESSION_SEND,
    TOOL_SESSION_STOP,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
)
from app.core.tool.schemas.request_tools import RequestTools
from app.core.tool.tool_registry import ToolRegistry
from app.models.session.session_kind import SessionType
from app.models.tool_result_facts import ToolResultFacts
from app.service.configuration.agents.builtin_agents import BUILTIN_CHILD_AGENTS
from tests.live_cold_support import LiveColdEnv, tool_call, tool_parts

_BROWSER = {
    "browser_navigate",
    "browser_snapshot",
    "browser_act",
    "browser_screenshot",
    "browser_tabs",
    "browser_logs",
    "browser_evaluate",
}
_SECOND_TIER = {
    TOOL_FORK_SESSION,
    TOOL_SESSION_SEND,
    TOOL_SESSION_LIST,
    TOOL_SESSION_READ,
    TOOL_SESSION_STOP,
    TOOL_PROPOSE_AGENT_CONFIGURATION,
}


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _probe_definition(seen: list[str]) -> ToolDefinition:
    def handler(*, execution_context: ToolExecutionContext) -> Any:
        seen.append(execution_context.tool_call_id)
        return tool_success("probe", "probed")

    return ToolDefinition(
        name="probe", group="test", description="Probe things", handler=handler, args_model=_NoArgs,
        deferred=True,
    ).normalized()


def _context(tmp_path: Path, *deferred: Any, loaded: Any = (), external: Any = ()) -> ToolExecutionContext:
    request_tools = RequestTools.derive(
        (), frozenset(), deferred_schemas=tuple(deferred), loaded_builtin=loaded, loaded_external=external
    )
    return ToolExecutionContext(
        session_id=1, workspace_id=1, workspace_root=tmp_path, step_id="s", run_id=1, tool_call_id="c1",
        request_tools=request_tools,
    )


class _NoExternal:
    def __init__(self) -> None:
        self.unavailable: str | None = None

    def catalog(self, workspace_root: str) -> tuple:
        if self.unavailable is not None:
            raise ExternalCatalogUnavailable(self.unavailable)
        return ()

    def instructions(self, workspace_root: str) -> tuple:
        return self.catalog(workspace_root)

    def execute(self, *_args: Any) -> Any:
        raise AssertionError("no external tools here")


@pytest.fixture
def external() -> Any:
    fake = _NoExternal()
    port.set_external_tools(fake)
    yield fake
    port.set_external_tools(None)


@pytest.fixture(autouse=True)
def _no_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_executor_module, "emit", lambda _event: None)


# —— 工具面 ——


@pytest.mark.usefixtures("backend_env")
def test_main_agent_surface_defers_browser_and_rare_collaboration_tools() -> None:
    surface = main_agent_tool_surface()
    names = [schema["name"] for schema in surface.schemas]
    assert set(surface.deferred) == _BROWSER | _SECOND_TIER
    assert TOOL_SEARCH in names and TOOL_SEARCH not in surface.deferred
    assert TOOL_READ_FILE in names and TOOL_READ_FILE not in surface.deferred


@pytest.mark.usefixtures("backend_env")
def test_deferred_tools_bring_tool_search_with_them() -> None:
    registry = get_tool_registry()
    read = registry.get_tool_definition(TOOL_READ_FILE)
    navigate = registry.get_tool_definition(TOOL_BROWSER_NAVIGATE)
    with_browser = freeze_tool_surface([read, navigate])
    assert [s["name"] for s in with_browser.schemas] == [TOOL_READ_FILE, TOOL_BROWSER_NAVIGATE, TOOL_SEARCH]
    assert with_browser.deferred == [TOOL_BROWSER_NAVIGATE]
    # 没有延迟工具就不补：tool_search 只在需要时出现（Agent 配置自己选了它除外）。
    assert [s["name"] for s in freeze_tool_surface([read]).schemas] == [TOOL_READ_FILE]


@pytest.mark.usefixtures("backend_env")
def test_child_agents_can_load_browser_and_mcp_tools() -> None:
    registry = get_tool_registry()
    for agent in BUILTIN_CHILD_AGENTS:
        assert TOOL_SEARCH in agent.allowed_tools, agent.agent_id
    general = next(agent for agent in BUILTIN_CHILD_AGENTS if agent.agent_id == "general-purpose")
    selected = [registry.get_tool_definition(name) for name in general.allowed_tools]
    surface = freeze_tool_surface(tools_for_session(SessionType.DELEGATED, selected, depth=1))
    assert set(surface.deferred) >= _BROWSER
    assert TOOL_PROPOSE_AGENT_CONFIGURATION not in {s["name"] for s in surface.schemas}


# —— 请求工具集与准入 ——


def test_loading_a_builtin_admits_it_without_touching_the_tool_list() -> None:
    base = ({"name": TOOL_READ_FILE, "strict": True}, {"name": TOOL_SEARCH, "strict": True})
    probe = {"name": "probe", "strict": True, "parameters": {}}
    other = {"name": "other", "strict": True, "parameters": {}}
    before = RequestTools.derive(base, frozenset(), deferred_schemas=(probe, other))
    assert [s["name"] for s in before.schemas] == [TOOL_READ_FILE, TOOL_SEARCH]
    assert before.unloaded == {"probe", "other"} and "probe" not in before.admitted

    after = RequestTools.derive(
        base, frozenset({"other"}), deferred_schemas=(probe, other), loaded_builtin={"probe", "other"}
    )
    assert after.schemas == base
    assert "probe" in after.admitted and "other" not in after.admitted
    assert after.unloaded == frozenset()


def test_calling_an_unloaded_builtin_says_to_load_it_first(tmp_path: Path) -> None:
    seen: list[str] = []
    definition = _probe_definition(seen)
    executor = ToolExecutor(ToolRegistry([definition]))
    schema = definition.to_model_tool_definition()
    call = ToolCall(tool_name="probe", arguments="{}", call_id="c1")

    before = RequestTools.derive((), frozenset(), deferred_schemas=(schema,))
    denied = executor.execute(call, _context(tmp_path), before)
    assert denied.status == "error" and '"select:probe"' in denied.content
    assert denied.display_data["status_hint"] == "工具未加载"

    after = RequestTools.derive((), frozenset(), deferred_schemas=(schema,), loaded_builtin={"probe"})
    assert executor.execute(call, _context(tmp_path), after).status == "success"
    assert seen == ["c1"]


# —— tool_search 与目录 ——


def test_tool_search_returns_the_frozen_definition_and_records_the_name(external, tmp_path) -> None:
    schema = _probe_definition([]).to_model_tool_definition()
    observation = ToolSearchTool().execute(
        query="select:probe", max_results=5, execution_context=_context(tmp_path, schema)
    )
    line = json.dumps(schema, ensure_ascii=False)
    assert observation.content == f"<functions>\n<function>{line}</function>\n</functions>\n{LOADED_NOTE}"
    assert observation.facts == ToolResultFacts(loaded_builtin_tools=("probe",))


def test_reloading_an_unchanged_tool_reminds_instead_of_returning_it_again(external, tmp_path) -> None:
    schema = _probe_definition([]).to_model_tool_definition()
    other = {**schema, "name": "other"}
    observation = ToolSearchTool().execute(
        query="select:probe,other",
        max_results=5,
        execution_context=_context(tmp_path, schema, other, loaded={"probe"}),
    )
    # 只给没加载过的那个；已加载的指回之前的结果，不再记一次加载。
    assert '"name": "other"' in observation.content and '"name": "probe"' not in observation.content
    assert "Already loaded" in observation.content and observation.content.endswith("probe")
    assert observation.facts == ToolResultFacts(loaded_builtin_tools=("other",))
    again = ToolSearchTool().execute(
        query="select:probe", max_results=5, execution_context=_context(tmp_path, schema, loaded={"probe"})
    )
    assert again.status == "success" and "<functions>" not in again.content and again.facts is None


def test_builtin_tools_stay_searchable_when_the_external_catalog_is_unreadable(external, tmp_path) -> None:
    external.unavailable = "external catalog is unavailable"
    schema = _probe_definition([]).to_model_tool_definition()
    found = ToolSearchTool().execute(query="probe", max_results=5, execution_context=_context(tmp_path, schema))
    assert found.status == "success" and found.facts.loaded_builtin_tools == ("probe",)
    missed = ToolSearchTool().execute(query="slack", max_results=5, execution_context=_context(tmp_path, schema))
    assert missed.status == "error" and "external catalog is unavailable" in missed.content


def test_catalog_lists_builtin_names_alongside_an_unreadable_external_catalog(external) -> None:
    schema = _probe_definition([]).to_model_tool_definition()
    context = SourceContext("/w", frozenset({TOOL_SEARCH}), None, None, (schema,))  # type: ignore[arg-type]
    source = DeferredToolsSource()
    assert "probe" in source.render(source.project(context))
    external.unavailable = "external catalog is unavailable"
    rendered = source.render(source.project(context))
    assert "probe" in rendered and "external catalog is unavailable" in rendered


# —— 完整链路 ——


def _tool_message(env: LiveColdEnv, call_id: str) -> str:
    for request in reversed(env.model.requests):
        for message in request:
            if message["role"] == "tool" and message["tool_call_id"] == call_id:
                return message["content"]
    raise AssertionError(f"no tool message for {call_id}")


def _search(call_id: str, name: str) -> tuple[list[dict[str, Any]], str]:
    args = {"query": f"select:{name}", "max_results": 5}
    return [{"tool_calls": [tool_call(call_id, TOOL_SEARCH, args)]}], "tool_calls"


def _call(call_id: str, name: str) -> tuple[list[dict[str, Any]], str]:
    return [{"tool_calls": [tool_call(call_id, name, {})]}], "tool_calls"


def _done() -> tuple[list[dict[str, Any]], str]:
    return [{"content": "完成"}], "stop"


async def test_a_deferred_builtin_is_listed_then_loaded_then_called(backend_env, tmp_path, monkeypatch) -> None:
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [_call("c0", "probe_deferred"), _search("c1", "probe_deferred"), _call("c2", "probe_deferred"), _done()],
    )
    try:
        env.send()
        run = await env.parked()

        assert run.status == "completed"
        first = env.model.tool_names[0]
        # 目录里有名字、请求里没有定义；浏览器一族同样只在目录里。
        assert "probe_deferred" not in first and not _BROWSER & set(first)
        assert "probe_deferred" in env.model.requests[0][0]["content"]
        # 加载后定义只在 tool 消息里：工具列表每次都一样，调用照样放行。
        assert all(names == first for names in env.model.tool_names)
        assert '"name": "probe_deferred"' in _tool_message(env, "c1")
        parts = {part["toolCallId"]: part for part in tool_parts(env.live())}
        assert parts["c0"]["status"] == "failed"
        assert parts["c2"]["status"] == "completed"
        env.assert_parity()
    finally:
        await env.close()


async def test_a_manual_fork_keeps_the_loaded_builtin(backend_env, tmp_path, monkeypatch) -> None:
    from app.service import depends

    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [_search("c1", "probe_deferred"), _done(), _call("c2", "probe_deferred"), _done()],
    )
    try:
        env.send()
        await env.parked()
        fork = await depends.get_session_service().fork_session(env.session.id, turn=1)
        env.send("在分支里用", session_id=fork.id)
        run = await env.parked(fork.id)

        assert run.status == "completed"
        assert "probe_deferred" not in env.model.tool_names[2]
        assert tool_parts(env.cold(fork.id))[-1]["status"] == "completed"
    finally:
        await env.close()
        await asyncio.sleep(0)
