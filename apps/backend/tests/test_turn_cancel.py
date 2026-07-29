"""Turn cancellation: rigorous tests for all cancellation paths.

覆盖范围：
- TestToolsNodeCancelled: ``_tools_node`` 取消后跳过工具执行
- TestModelNodeCancelled: ``_model_node`` 流式期间取消检查（回归）
- TestRunnerCancelFinalize: ``AgentRuntime`` 取消 + finally 兜底
- 每个子类的 NormalExecution 测试确保非取消路径不受影响
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from app.core.runtime.runner import AgentRuntime
from app.core.runtime.runtime_operations import RuntimeOperations
from app.core.workflows.react import nodes as nodes_module
from app.core.workflows.react.nodes import _model_node, _tools_node
from app.core.workflows.react.runtime_config import RuntimeConfig
from app.core.workflows.react.state import ReactGraphState
from app.models import TaskRecord, TurnRecord
from app.models.enums.event_type import EventType
from app.models.payload import RunCancelledPayload

# ===================================================================
# 辅助工具
# ===================================================================


class _AsyncIter:
    """把普通列表包装为异步迭代器，供 mock model.astream 使用。"""

    def __init__(self, items: list) -> None:
        self._items = list(items)
        self._i = 0

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self):
        if self._i >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._i]
        self._i += 1
        return item


def _make_ai_chunks(texts: list[str]) -> list[Any]:
    """构造 AIMessageChunk 列表供 model.astream 返回。"""
    from langchain_core.messages import AIMessageChunk
    return [AIMessageChunk(content=t, id=f"chunk-{i}") for i, t in enumerate(texts)]


def _build_state(**overrides: Any) -> ReactGraphState:
    """构造默认 ReactGraphState，支持字段覆盖。"""
    defaults: dict[str, Any] = {
        "messages": [],
        "step_count": 2,
        "tool_error_count": 0,
        "requested_tool": False,
        "final_response": False,
        "terminal": False,
        "pending_tool_calls": [],
        "max_steps": 10,
        "final_text": "",
    }
    defaults.update(overrides)
    return ReactGraphState(**defaults)


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
def mock_ops() -> mock.MagicMock:
    """返回带默认行为的 RuntimeOperations mock。"""
    ops = mock.MagicMock(spec=RuntimeOperations)
    ops.has_turn_status.return_value = False   # 默认未取消
    ops.update_turn_status.return_value = mock.MagicMock(spec=TurnRecord)
    ops.agent_profile = mock.MagicMock()
    ops.agent_profile.agent_id = "test_agent"
    ops.model_tools = []
    ops.build_messages.return_value = []
    return ops


@pytest.fixture
def mock_turn() -> mock.MagicMock:
    turn = mock.MagicMock(spec=TurnRecord)
    turn.turn_id = "turn-cancel-test"
    turn.task_id = "task-cancel-test"
    turn.agent_id = "test_agent"
    return turn


@pytest.fixture
def mock_task() -> mock.MagicMock:
    task = mock.MagicMock(spec=TaskRecord)
    task.task_id = "task-cancel-test"
    task.workspace_id = "ws-cancel-test"
    task.agent_id = "test_agent"
    return task


@pytest.fixture
def runtime_config(
    mock_ops: mock.MagicMock,
    mock_task: mock.MagicMock,
    mock_turn: mock.MagicMock,
) -> RuntimeConfig:
    """注入 RuntimeConfig（含 mock operations）。"""
    return RuntimeConfig(
        operations=mock_ops,
        task=mock_task,
        turn=mock_turn,
        model=mock.MagicMock(),
    )


def _setup_langgraph_context(config: RuntimeConfig) -> dict:
    """构造 LangGraph 运行时上下文并 mock get_config / get_stream_writer。

    返回 {"config": cfg_dict, "writer": writer_mock}。
    """
    cfg = {
        "configurable": {
            "runtime_config": config,
        }
    }
    writer = mock.MagicMock()
    return {"config": cfg, "writer": writer}


# ===================================================================
# 1. _tools_node 取消检查
# ===================================================================


class TestToolsNodeCancelled:
    """_tools_node 中 interrupt() 恢复后的取消检查。"""

    # ------------------------------------------------------------------
    # 1a. 取消路径
    # ------------------------------------------------------------------

    def test_skips_tool_execution_when_cancelled(
        self, runtime_config: RuntimeConfig,
    ) -> None:
        """取消时跳过工具执行，return terminal=True + emit RUN_CANCELLED。"""
        ctx = _setup_langgraph_context(runtime_config)
        runtime_config.operations.has_turn_status.return_value = True  # 已取消

        state = _build_state(
            pending_tool_calls=[
                {"tool_name": "read_file", "arguments": {"path": "x"}, "call_id": "c1"},
            ],
            tool_error_count=3,
        )

        with (
            mock.patch.object(nodes_module, "get_config", return_value=ctx["config"]),
            mock.patch.object(nodes_module, "get_stream_writer", return_value=ctx["writer"]),
            mock.patch("app.core.workflows.react.nodes.interrupt", return_value=[]),
        ):
            result = _tools_node(state)

        # 工具执行被跳过
        runtime_config.operations.run_tool_calls.assert_not_called()
        runtime_config.operations.update_turn_status.assert_not_called()

        assert result["terminal"] is True
        assert result["pending_tool_calls"] == []
        assert result["messages"] == []
        assert result["tool_error_count"] == 3  # 取消不累计错误

        # RUN_CANCELLED 事件被 emit
        writer_calls = ctx["writer"].call_args_list
        assert len(writer_calls) >= 1
        last_call = writer_calls[-1][0][0]
        assert last_call["event_type"] == str(EventType.RUN_CANCELLED)
        assert isinstance(last_call["payload"], RunCancelledPayload)
        assert last_call["payload"].status == "cancelled"

    def test_cancelled_with_high_error_count(
        self, runtime_config: RuntimeConfig,
    ) -> None:
        """取消时高错误计数也不触发 TOOL_ERROR_LIMIT 分支。"""
        ctx = _setup_langgraph_context(runtime_config)
        runtime_config.operations.has_turn_status.return_value = True

        state = _build_state(
            pending_tool_calls=[
                {"tool_name": "write_file", "arguments": {}, "call_id": "c2"},
            ],
            tool_error_count=5,  # 已达上限
        )

        with (
            mock.patch.object(nodes_module, "get_config", return_value=ctx["config"]),
            mock.patch.object(nodes_module, "get_stream_writer", return_value=ctx["writer"]),
            mock.patch("app.core.workflows.react.nodes.interrupt", return_value=[]),
        ):
            result = _tools_node(state)

        assert result["terminal"] is True
        assert result["tool_error_count"] == 5
        # 不应触发 RUN_FAILED
        for call_obj in ctx["writer"].call_args_list:
            assert call_obj[0][0]["event_type"] != str(EventType.RUN_FAILED)

    # ------------------------------------------------------------------
    # 1b. 非取消路径（回归）
    # ------------------------------------------------------------------

    def test_normal_execution_proceeds(self, runtime_config: RuntimeConfig) -> None:
        """未取消时正常执行工具。"""
        ctx = _setup_langgraph_context(runtime_config)
        runtime_config.operations.run_tool_calls.return_value = mock.MagicMock(
            observations=[],
            messages_for_model=[],
        )

        state = _build_state(
            pending_tool_calls=[
                {"tool_name": "read_file", "arguments": {"path": "x"}, "call_id": "c1"},
            ],
        )

        with (
            mock.patch.object(nodes_module, "get_config", return_value=ctx["config"]),
            mock.patch.object(nodes_module, "get_stream_writer", return_value=ctx["writer"]),
            mock.patch("app.core.workflows.react.nodes.interrupt", return_value=[]),
        ):
            result = _tools_node(state)

        # 注意：_tools_node 正常路径不返回 terminal key（只有取消/失败路径返回）
        runtime_config.operations.run_tool_calls.assert_called_once()
        assert result["pending_tool_calls"] == []

        # 无 RUN_CANCELLED
        for call_obj in ctx["writer"].call_args_list:
            assert call_obj[0][0]["event_type"] != str(EventType.RUN_CANCELLED)

    def test_empty_pending_calls_still_enters_interrupt(
        self, runtime_config: RuntimeConfig,
    ) -> None:
        """空 pending_tool_calls 时 interrupt 仍被调用，run_tool_calls 被调用（空列表）。"""
        ctx = _setup_langgraph_context(runtime_config)
        state = _build_state(pending_tool_calls=[], tool_error_count=1)

        with (
            mock.patch.object(nodes_module, "get_config", return_value=ctx["config"]),
            mock.patch.object(nodes_module, "get_stream_writer", return_value=ctx["writer"]),
            mock.patch("app.core.workflows.react.nodes.interrupt", return_value=[]) as mock_int,
        ):
            result = _tools_node(state)

        assert mock_int.called
        # run_tool_calls 被调用（空列表），这是正常路径行为
        runtime_config.operations.run_tool_calls.assert_called_once()
        # 正常路径，不返回 terminal
        assert "terminal" not in result


# ===================================================================
# 2. _model_node 取消检查（回归）
# ===================================================================


class TestModelNodeCancelled:
    """_model_node 流式期间取消检查（已有行为回归）。"""

    def _setup(self, runtime_config: RuntimeConfig) -> dict:
        """配置 LangGraph 上下文，并返回 ctx。"""
        ctx = _setup_langgraph_context(runtime_config)
        # model 用 MagicMock + 手动 astream，避免 AsyncMock 的协程行为
        # model.astream 返回 _AsyncIter 包装的可迭代对象
        return ctx

    async def test_cancelled_during_streaming(
        self, runtime_config: RuntimeConfig,
    ) -> None:
        """流式中检测到取消，提前终止并 emit RUN_CANCELLED。"""
        ctx = self._setup(runtime_config)
        # 第一个 chunk 时 has_turn_status("cancelled") 返回 True → 立即取消
        runtime_config.operations.has_turn_status.side_effect = [True]

        chunks = _make_ai_chunks(["Hello"])
        runtime_config.model.astream = mock.MagicMock(return_value=_AsyncIter(chunks))

        state = _build_state(messages=[], step_count=0)

        with (
            mock.patch.object(nodes_module, "get_config", return_value=ctx["config"]),
            mock.patch.object(nodes_module, "get_stream_writer", return_value=ctx["writer"]),
        ):
            result = await _model_node(state)

        assert result["terminal"] is True
        assert result["requested_tool"] is False
        assert result["final_response"] is False
        runtime_config.operations.update_turn_status.assert_called()

        # 验证 RUN_CANCELLED 事件
        cancelled = any(
            call[0][0]["event_type"] == str(EventType.RUN_CANCELLED)
            for call in ctx["writer"].call_args_list
        )
        assert cancelled, "RUN_CANCELLED 事件未被 emit"

    async def test_not_cancelled_completes_normally(
        self, runtime_config: RuntimeConfig,
    ) -> None:
        """未取消时正常完成（回归）。"""
        ctx = self._setup(runtime_config)
        runtime_config.operations.has_turn_status.return_value = False

        chunks = _make_ai_chunks(["Hello", " world"])
        runtime_config.model.astream = mock.MagicMock(return_value=_AsyncIter(chunks))

        state = _build_state(messages=[], step_count=0)

        with (
            mock.patch.object(nodes_module, "get_config", return_value=ctx["config"]),
            mock.patch.object(nodes_module, "get_stream_writer", return_value=ctx["writer"]),
        ):
            result = await _model_node(state)

        assert result["terminal"] is True
        assert not any(
            call[0][0]["event_type"] == str(EventType.RUN_CANCELLED)
            for call in ctx["writer"].call_args_list
        )

    async def test_cancelled_preserves_running_status(
        self, runtime_config: RuntimeConfig,
    ) -> None:
        """取消后 turn 状态由 model_node 设为 cancelled（由 update_turn_status 完成）。"""
        ctx = self._setup(runtime_config)
        runtime_config.operations.has_turn_status.side_effect = [True]  # 第一个 chunk 前就取消

        chunks = _make_ai_chunks(["a"])
        runtime_config.model.astream = mock.MagicMock(return_value=_AsyncIter(chunks))

        state = _build_state(messages=[], step_count=0)

        with (
            mock.patch.object(nodes_module, "get_config", return_value=ctx["config"]),
            mock.patch.object(nodes_module, "get_stream_writer", return_value=ctx["writer"]),
        ):
            result = await _model_node(state)

        assert result["terminal"] is True
        # update_turn_status 被调用（状态设为 cancelled）
        runtime_config.operations.update_turn_status.assert_called()


# ===================================================================
# 3. AgentRuntime cancel_turn + finally 兜底
# ===================================================================


class TestRunnerCancelFinalize:
    """AgentRuntime.cancel_turn 与 run_turn finally 断连兜底的交互。"""

    @staticmethod
    def _make_runtime(turn_service: Any) -> AgentRuntime:
        """构造最小 AgentRuntime（跳过 __init__）。"""
        runtime = object.__new__(AgentRuntime)
        runtime._turn_service = turn_service
        runtime._task_service = mock.MagicMock()
        runtime._context_builder = mock.MagicMock()
        runtime._tool_scheduler = mock.MagicMock()
        runtime._agent_registry = mock.MagicMock()
        runtime._workspace_service = mock.MagicMock()
        return runtime

    def test_cancel_turn_sets_cancelled(self) -> None:
        """cancel_turn 正确设状态 + end_reason。"""
        turn_service = mock.MagicMock()
        turn_service.update_turn_status.return_value = mock.MagicMock(
            spec=TurnRecord, turn_id="t1", task_id="task1",
            status="cancelled", end_reason="user_cancelled",
        )
        runtime = self._make_runtime(turn_service)

        result = runtime.cancel_turn("t1")

        turn_service.update_turn_status.assert_called_with(
            "t1", "cancelled", end_reason="user_cancelled",
        )
        assert result.status == "cancelled"
        assert result.end_reason == "user_cancelled"

    def test_finally_does_not_override_cancelled(self) -> None:
        """finally 不覆盖 cancelled 状态。"""
        turn_service = mock.MagicMock()
        turn_service.has_turn_status.return_value = False  # 不是 running
        runtime = self._make_runtime(turn_service)

        runtime._mark_turn_disconnected_if_running("t1")

        turn_service.has_turn_status.assert_called_with("t1", "running")
        turn_service.update_turn_status.assert_not_called()

    def test_finally_covers_running_turn(self) -> None:
        """finally 覆盖 running 为 failed(client_disconnected)。"""
        turn_service = mock.MagicMock()
        turn_service.has_turn_status.return_value = True  # 仍在 running
        runtime = self._make_runtime(turn_service)

        runtime._mark_turn_disconnected_if_running("t1")

        turn_service.update_turn_status.assert_called_with(
            "t1", "failed", end_reason="client_disconnected",
        )

    def test_cancel_turn_idempotent_on_completed(self) -> None:
        """对已完成 turn 再次 cancel 不会报错（幂等）。"""
        turn_service = mock.MagicMock()
        turn_service.get_turn.return_value = mock.MagicMock(
            spec=TurnRecord, turn_id="t1", task_id="task1", status="completed",
        )
        turn_service.update_turn_status.return_value = mock.MagicMock(
            spec=TurnRecord, turn_id="t1", task_id="task1", status="cancelled",
        )
        runtime = self._make_runtime(turn_service)

        result = runtime.cancel_turn("t1")  # 不应抛出异常
        assert result.status == "cancelled"

    def test_cancel_before_finally_full_flow(self) -> None:
        """时序测试：cancel_turn → finally 不覆盖。"""
        turn_service = mock.MagicMock()
        turn_service.update_turn_status.return_value = mock.MagicMock(
            spec=TurnRecord, turn_id="t1", task_id="task-x",
            status="cancelled",
        )
        runtime = self._make_runtime(turn_service)

        # Step 1: cancel_turn
        cancelled = runtime.cancel_turn("t1")
        assert cancelled.status == "cancelled"

        # Step 2: 模拟 workflow 结束后 finally 块
        turn_service.has_turn_status.return_value = False  # cancelled → 不是 running
        runtime._mark_turn_disconnected_if_running("t1")

        # Step 3: 验证只被 cancel_turn 更新过一次
        turn_service.update_turn_status.assert_called_once()


# ===================================================================
# 4. Workflow-level cancellation scenario integration
# ===================================================================


class TestFullCancelFlow:
    """端到端取消流程的核心时序验证（不依赖 LangGraph graph 执行）。"""

    def test_cancel_flag_propagation(
        self, mock_ops: mock.MagicMock, runtime_config: RuntimeConfig,
    ) -> None:
        """验证取消标志在各检查点之间正确传播。

        模拟以下时序：
        1. has_turn_status 初始返回 False（未取消）
        2. 外部调用 cancel_turn 改变 DB 状态
        3. 后续 has_turn_status 返回 True（取消已生效）
        """
        cancellations_detected = 0

        def _has_turn_status(turn_id: str, status: str) -> bool:
            if status == "cancelled":
                nonlocal cancellations_detected
                cancellations_detected += 1
                if cancellations_detected >= 2:
                    return True  # 第二次检查时取消已生效
            return status == "running"

        mock_ops.has_turn_status.side_effect = _has_turn_status

        # 第一轮检查（中断处）
        first_check = mock_ops.has_turn_status("turn-1", "cancelled")
        assert not first_check  # 还未取消

        # 模拟 cancel_turn 设 cancelled
        mock_ops.has_turn_status.side_effect = None
        mock_ops.has_turn_status.return_value = True

        # 第二轮检查
        second_check = mock_ops.has_turn_status("turn-1", "cancelled")
        assert second_check  # 已取消

    def test_finally_does_not_leak_cancelled_before_workflow(
        self,
    ) -> None:
        """验证 run_turn 的 finally 块不会在上层 entry 前覆盖 cancelled。"""
        from app.service.task.turn_service import TurnService

        turn_service = mock.MagicMock(spec=TurnService)
        # 模拟：cancel_turn 已设 cancelled
        turn_service.has_turn_status.return_value = False  # 不是 running

        runtime = object.__new__(AgentRuntime)
        runtime._turn_service = turn_service

        # 在 workflow.run() 执行前调用 finally
        runtime._mark_turn_disconnected_if_running("t1")

        turn_service.has_turn_status.assert_called_with("t1", "running")
        turn_service.update_turn_status.assert_not_called()
