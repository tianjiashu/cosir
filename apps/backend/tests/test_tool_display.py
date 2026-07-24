"""ToolDisplayHints 与事件展示元数据的单元测试。

覆盖三类不变量：
- ``ToolDisplayHints.render`` 的分页派生、click_action 结构化、缺省与缺字段降级。
- ``build_read_file_definition`` 在工具契约里声明了正确的展示提示。
- ``RuntimeOperations.build_tool_call_requested`` 把 ``ToolDefinition.display``
  投影进 ``TOOL_CALL_REQUESTED`` 事件 payload；未知工具或工具未声明 display 时
  降级为 ``display=None``，保证前端通用渲染可安全降级。
"""

from types import SimpleNamespace

from app.core.runtime.runtime_operations import RuntimeOperations
from app.models.payload.tool_call_requested_payload import ToolCallRequestedPayload
from app.tools.schemas import ToolCall, ToolDefinition
from app.tools.schemas.tool_display import ToolDisplayHints
from app.tools.tool_handler.read_file import build_read_file_definition


def _make_operations(model_tools: list[ToolDefinition]) -> RuntimeOperations:
    """用最小替身构造 ``RuntimeOperations``，避免拉起完整运行时依赖。

    参数:
        model_tools: 注入到门面的工具定义列表（仅 ``build_tool_call_requested`` 使用）。

    返回:
        仅用于测试门面投影逻辑的 ``RuntimeOperations`` 实例。

    异常:
        无。

    副作用:
        无。
    """

    return RuntimeOperations(
        settings=SimpleNamespace(),
        turn_store=SimpleNamespace(),
        context_builder=SimpleNamespace(),
        tool_scheduler=SimpleNamespace(),
        agent_profile=SimpleNamespace(agent_id="dev"),
        model_tools=model_tools,
    )


def test_render_paginated_summary_derives_line_range() -> None:
    """分页类工具应自动派生 ``L{start}-L{end}`` 并渲染 click_action。"""

    hints = ToolDisplayHints(
        verb="读取",
        icon="eye",
        summary_template="{path} · L{start}-L{end}",
        detail_keys=("path", "offset", "limit"),
        click_action="open_file:{path}",
    )
    out = hints.render({"path": "x.ts", "offset": 5, "limit": 10})

    assert out["verb"] == "读取"
    assert out["icon"] == "eye"
    assert out["summary"] == "x.ts · L5-L14"
    assert out["detail_keys"] == ["path", "offset", "limit"]
    assert out["click_action"] == {"action": "open_file", "target": "x.ts"}


def test_render_without_template_falls_back_to_verb_and_primary() -> None:
    """没有 summary_template 时降级为 ``verb + 主参数``。"""

    hints = ToolDisplayHints(verb="搜索", icon="search", detail_keys=("query",))
    out = hints.render({"query": "foo"})

    assert out["summary"] == "搜索 foo"
    assert out["click_action"] is None


def test_render_missing_template_field_falls_back() -> None:
    """summary_template 引用了不存在的字段时应安全降级，不抛出。"""

    hints = ToolDisplayHints(
        verb="搜索", icon="search", summary_template="{missing}", detail_keys=("query",)
    )
    out = hints.render({"query": "foo"})

    assert out["summary"] == "搜索 foo"


def test_render_invalid_click_action_is_none() -> None:
    """click_action 模板缺少 ``<action>:<target>`` 分隔时降级为 None。"""

    hints = ToolDisplayHints(verb="v", icon="i", click_action="badtemplate")
    out = hints.render({})

    assert out["click_action"] is None


def test_read_file_definition_declares_display() -> None:
    """read_file 的工具定义必须声明展示提示（含可点击打开文件）。"""

    definition = build_read_file_definition(".")

    assert definition.display is not None
    assert definition.display.verb == "读取"
    assert definition.display.icon == "eye"
    assert definition.display.detail_keys == ("path", "offset", "limit")
    assert definition.display.click_action == "open_file:{path}"


def test_build_tool_call_requested_renders_display() -> None:
    """门面应把已知工具的 display 投影进 TOOL_CALL_REQUESTED payload。"""

    operations = _make_operations([build_read_file_definition(".")])
    call = ToolCall(
        tool_name="read_file",
        arguments={"path": "a.ts", "offset": 1, "limit": 20},
        call_id="c1",
    )

    payload = operations.build_tool_call_requested(call, "step-1")

    assert isinstance(payload, ToolCallRequestedPayload)
    assert payload.tool_name == "read_file"
    assert payload.tool_call_id == "c1"
    assert payload.step_id == "step-1"
    assert payload.display == {
        "verb": "读取",
        "icon": "eye",
        "summary": "a.ts · L1-L20",
        "detail_keys": ["path", "offset", "limit"],
        "click_action": {"action": "open_file", "target": "a.ts"},
    }


def test_build_tool_call_requested_unknown_tool_display_none() -> None:
    """未知工具或未声明 display 时，payload.display 必须为 None，保证前端降级。"""

    unknown = ToolDefinition(
        name="ghost",
        description="d",
        permission="p",
        required_params=(),
        handler=lambda: None,
    )
    operations = _make_operations([unknown])
    call = ToolCall(tool_name="ghost", arguments={}, call_id="c2")

    payload = operations.build_tool_call_requested(call, "step-2")

    assert payload.display is None
