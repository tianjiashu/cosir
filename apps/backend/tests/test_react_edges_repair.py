"""``_should_continue`` 条件边路由单元测试（invalid_tool_calls 自愈 Task 3 测试点 4）。

只覆盖 ``react/edges.py`` 的路由判定，不触发任何节点执行。字段口径按当前契约：
``repair_requested`` 是 **str**（``"true"`` / ``"false"``），不是 bool。
"""

from __future__ import annotations

from langgraph.graph import END

from app.core.workflows.react.edges import _should_continue
from app.core.workflows.react.state import ReactGraphState


def _make_state(
    *,
    repair_requested: str = "false",
    requested_tool: bool = False,
    terminal: bool = False,
    final_response: bool = False,
    step_count: int = 1,
    max_steps: int = 10,
) -> ReactGraphState:
    """构造仅用于路由判定的最小 graph state。

    参数:
        repair_requested: 修复回流标志（str 口径，``"true"`` 表示需回流 model）。
        requested_tool: 本步是否请求了合法工具。
        terminal: 是否已终态。
        final_response: 是否已产出最终回复。
        step_count: 当前步数。
        max_steps: 步数上限。

    返回:
        字段完整的 ``ReactGraphState``。

    异常:
        无。

    副作用:
        无。
    """

    return ReactGraphState(
        step_count=step_count,
        tool_error_count=0,
        requested_tool=requested_tool,
        repair_requested=repair_requested,
        final_response=final_response,
        terminal=terminal,
        pending_tool_calls=[],
        max_steps=max_steps,
        final_text="",
        last_tool_results=[],
    )


def test_should_continue_repair_requested_returns_model() -> None:
    """测试目的：``repair_requested="true"`` 且非 terminal/非 final → "model"（回流可达）。

    可能发现的缺陷：回流分支缺失或读的是 bool 字段 → graph 直接 END、情形 b
    自愈完全失效（plan §0.1 #11）。
    """

    assert _should_continue(_make_state(repair_requested="true")) == "model"


def test_should_continue_requested_tool_returns_tools() -> None:
    """测试目的：``repair_requested="false"`` + ``requested_tool=True`` → "tools"。

    可能发现的缺陷：用非空串判定 ``repair_requested``（``"false"`` 也非空 → 恒真）抢占了
    工具分支 → 合法工具永不执行，graph 空转回流 model（plan §0.1 #11 核心 bug）。
    """

    state = _make_state(repair_requested="false", requested_tool=True)

    assert _should_continue(state) == "tools"


def test_should_continue_no_request_returns_end() -> None:
    """测试目的：``repair_requested="false"`` + ``requested_tool=False`` + 非 terminal → END。

    可能发现的缺陷：非空串判定使 ``"false"`` 被视为真 → 无任何新意图时回流 model
    死循环空转（同 #11）。
    """

    state = _make_state(repair_requested="false", requested_tool=False)

    assert _should_continue(state) == END


def test_should_continue_max_steps_overrides_repair() -> None:
    """测试目的：``repair_requested="true"`` + step>=max_steps → "max_steps"（兜底拦截）。

    可能发现的缺陷：步数拦截排在修复分支之后 → 修复回流绕过上限，无限循环烧 token。
    """

    state = _make_state(repair_requested="true", step_count=3, max_steps=3)

    assert _should_continue(state) == "max_steps"


def test_should_continue_terminal_or_final_returns_end() -> None:
    """测试目的：terminal/final_response → END 优先（回流与步数拦截都不生效）。

    可能发现的缺陷：终态判定排在修复/拦截分支之后 → 已终态仍回流 model，产生
    终态后的多余模型调用与重复事件。
    """

    terminal_state = _make_state(repair_requested="true", terminal=True)
    final_state = _make_state(repair_requested="true", final_response=True)

    assert _should_continue(terminal_state) == END
    assert _should_continue(final_state) == END


def test_empty_repair_requested_string_is_falsy_and_routes_to_end() -> None:
    """测试目的：``repair_requested=""``（空串）按假值处理 → 不回流。

    可能发现的缺陷：把非空判断写成 ``is not None`` → 空串被当真值触发无意义回流。
    """

    state = _make_state(repair_requested="", requested_tool=False)

    assert _should_continue(state) == END
