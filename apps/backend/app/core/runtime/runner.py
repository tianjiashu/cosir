"""Coordinate task lifecycle and workflow execution."""

import logging
import os
from collections.abc import AsyncIterator
from uuid import uuid4

from langchain_core.messages import BaseMessage

from app.config.logging import (
    shutdown_logging,
    trace_log_extra,
)
from app.config.logging.logger import log
from app.config.settings import BackendSettings
from app.core.agents.profile import AgentProfile, default_developer_agent
from app.core.context import TextContextBuilder
from app.core.runtime.runs.checkpointer import build_checkpointer
from app.core.runtime.runtime_operations import RuntimeOperations
from app.models import TaskRecord, TurnRecord
from app.models.enums.event_type import EventType
from app.models.runtime_event import RuntimeEvent
from app.models.runtime_message import RuntimeMessage
from app.models.trace_context import TraceContext
from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService
from app.tools.schemas import ToolDefinition
from app.tools.tool_execute.tool_scheduler import ToolScheduler


class AgentRuntime:
    """Execute tasks and stream runtime events.

    单一职责：作为执行 / 生命周期引擎，负责任务状态推进、模型流消费、工具调度、
    运行时事件记录、取消与终止保护，以及从 LangGraph checkpoint 派生事件。

    状态单一事实来源是 ``Turn``：本引擎只写 turn 执行态，task 执行态由最新 turn 派生；
    取消作用于 turn 并中止该 turn 的运行循环（模型节点检查 turn 取消状态后停止派发工具）。

    职责边界：
    - 负责：任务执行编排、运行时事件、取消。
    - 不负责：checkpoint 回放与历史事件回看（已移除；历史由 ``GET /tasks/{task_id}/turns``
      提供，事件由 LangGraph checkpoint 承载）、工作区 / 任务 / 轮次的 CRUD 与查询
      （委托给对应 service 层）；不对外暴露 service 访问器，service 仅作为本引擎的私有协作者。
    """

    def __init__(
        self,
        settings: BackendSettings,
        task_service: TaskService,
        turn_service: TurnService,
        context_builder: TextContextBuilder,
        tool_scheduler: ToolScheduler,
        agent_profile: AgentProfile | None = None,
        model_tools: list[ToolDefinition] | None = None,
    ) -> None:
        """Initialize the execution engine with its private collaborators.

        参数:
            settings: 后端运行配置。
            task_service: 任务编排服务（私有协作者，不对外暴露）。
            turn_service: 轮次编排服务（私有协作者，不对外暴露）。
            context_builder: 文本上下文构建器。
            tool_scheduler: 工具调度器。
            logger: 运行时日志器。
            agent_profile: 可选 Agent 角色配置。
            model_tools: 暴露给模型的工具定义列表。
        """

        self._settings = settings
        self._task_service = task_service
        self._turn_service = turn_service
        self._context_builder = context_builder
        self._tool_scheduler = tool_scheduler
        self._agent_profile = agent_profile or default_developer_agent()
        self._model_tools = list(model_tools or [])

    def close(self) -> None:
        """Close external resources held by the runtime."""

        shutdown_logging(self._logger.name)

    def cancel_turn(self, turn_id: str) -> TurnRecord:
        """Cancel a turn and mark it cancelled.

        置该 turn 为 ``cancelled``（带终态原因），并 emit ``RUN_CANCELLED`` 事件。模型节点在
        下一轮循环检查到 turn 已取消会停止派发工具，从而中止该 turn 的运行（满足「取消要中止
        工具运行」的编排层语义）。

        参数:
            turn_id: 待取消的轮次标识。

        返回:
            取消后的 ``TurnRecord``。
        """

        turn = self._turn_service.update_turn_status(
            turn_id, "cancelled", end_reason="user_cancelled"
        )
        self._record(
            EventType.RUN_CANCELLED,
            turn.task_id,
            {"status": "cancelled", "_turn_id": turn_id},
        )
        self._logger.info(
            "turn_cancelled",
            extra={
                "msg": "turn cancelled",
                "data": {"turn_id": turn_id, "task_id": turn.task_id},
            },
        )
        return turn

    async def run_turn(
        self,
        turn_id: str,
        turn: TurnRecord | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """执行单个 pending 轮次并实时流式产出运行时事件。

        只负责「pending → 认领 → 执行 → 流式事件」。历史回看与断线重连不属于本方法职责：
        客户端在打开任务时通过 ``GET /tasks/{task_id}/turns`` 拉取完整历史对话，SSE 通过
        断开兜底（finally 标记 failed）判定本轮是否中断。非 pending 轮次不应进入本方法，
        调用方（API 层）应先做 409 守卫；此处仅做防御性早退。

        参数:
            turn_id: 需要运行的轮次标识符。
            turn: 可选的预取轮次记录；缺省时按 ``turn_id`` 读取。
            model: 可选注入的 LangChain chat model；缺省时由工作流按配置构建。
        """

        if turn is None:
            turn = self._turn_service.get_turn(turn_id)
        # 测试注入模型优先；否则由工作流按配置构建（echo/无 Key 时为离线模型）。
        task_id = turn.task_id
        task = self._task_service.get_task(task_id)

        if turn.status != "pending":
            log.warning(
                "run_turn_non_pending",
                extra={
                    "msg": "run_turn called for non-pending turn; refusing to execute",
                    "data": {"turn_id": turn_id, "status": turn.status},
                },
            )
            return

        agent_profile = self._resolve_task_agent_profile(task)
        if agent_profile is None:
            self._turn_service.update_turn_status(
                turn.turn_id, "failed", end_reason="agent_profile_unavailable"
            )
            log.error(
                "agent_profile_unavailable",
                extra=trace_log_extra(
                    TraceContext(trace_id=str(uuid4()), task_id=task_id),
                    msg="agent profile unavailable for task",
                    data={
                        "task_id": task_id,
                        "task_agent_id": task.agent_id,
                        "runtime_agent_id": self._agent_profile.agent_id,
                    },
                ),
            )
            yield self._record(
                EventType.RUN_FAILED,
                task_id,
                {
                    "status": "failed",
                    "error": "agent_profile_unavailable",
                    "task_agent_id": task.agent_id,
                    "runtime_agent_id": self._agent_profile.agent_id,
                    "_turn_id": turn.turn_id,
                },
            )
            return

        if not self._turn_service.claim_pending_turn(turn.turn_id):
            # 已被其它连接抢占（极小概率的竞态）：本轮不再重复驱动，直接退出。
            log.warning(
                "turn_claim_lost",
                extra={
                    "msg": "turn already claimed by another connection",
                    "data": {"turn_id": turn.turn_id},
                },
            )
            return

        yield self._record(
            EventType.RUN_STARTED,
            task_id,
            {"status": "running", "agent": agent_profile.to_dict(), "_turn_id": turn.turn_id},
        )

        operations = RuntimeOperations(
            settings=self._settings,
            turn_store=self._turn_service,
            context_builder=self._context_builder,
            tool_scheduler=self._tool_scheduler,
            agent_profile=agent_profile,
            current_turn_id=turn.turn_id,
            model_tools=self._model_tools,
        )

        try:
            async for event in agent_profile.workflow.run(task, operations):
                yield event
            await self._persist_turn_trajectory(turn.turn_id)
            return
        except Exception as exc:
            self._turn_service.update_turn_status(
                turn.turn_id, "failed", end_reason=str(exc)
            )
            log.exception(
                "task_failed",
                extra={
                    "msg": "task execution failed",
                    "data": {"task_id": task_id},
                },
            )
            event = RuntimeEvent(
                event_type=EventType.RUN_FAILED,
                task_id=task_id,
                turn_id=turn.turn_id,
                payload={"status": "failed", "error": str(exc)},
            )
            yield event

    async def _persist_turn_trajectory(self, turn_id: str) -> None:
        """Persist the turn's message trajectory for cross-turn memory.

        读取该 turn（thread_id = turn_id）checkpoint 的最终 ``messages``，转换为模型无关的
        ``RuntimeMessage`` 列表并落库，供下一轮构建上下文时拼回。

        参数:
            turn_id: 待持久化轨迹的轮次标识（同时作为 checkpoint thread_id）。

        返回:
            无。

        异常:
            读取或转换失败时仅记日志，不向上抛出（轨迹持久化失败不应中断运行）。
        """

        try:
            async with build_checkpointer() as checkpointer:
                state = await checkpointer.aget_state(
                    {"configurable": {"thread_id": turn_id}}
                )
            if state is None or not state.values:
                return
            messages = state.values.get("messages") or []
            runtime_messages = _langchain_messages_to_runtime(messages)
            self._turn_service.save_turn_messages(turn_id, runtime_messages)
        except Exception:
            self._logger.exception(
                "turn_trajectory_persist_failed",
                extra={
                    "msg": "failed to persist turn message trajectory",
                    "data": {"turn_id": turn_id},
                },
            )

    def backend_health(self) -> dict:
        """Return backend model configuration and availability summary."""

        return {
            "status": "ok",
            "model_provider": self._settings.model_provider,
            "model_base_url": self._settings.model_base_url,
            "model_name": self._settings.model_name,
            "model_thinking_mode": self._settings.model_thinking_mode,
            "model_api_key_env": self._settings.model_api_key_env,
            "has_model_api_key": bool(os.environ.get(self._settings.model_api_key_env)),
        }

    def _resolve_task_agent_profile(self, task: TaskRecord) -> AgentProfile | None:
        """Resolve the agent profile allowed to execute a task.
            TODO:后续需要改造,_agent_profile 要和谁绑定呢？应该是请求吧？请求使用哪个_agent_profile？
        """

        if task.agent_id == self._agent_profile.agent_id:
            return self._agent_profile
        return None

    def _record(self, event_type: EventType, task_id: str, payload: dict) -> RuntimeEvent:
        """创建一条运行时事件。

        参数:
            event_type: 稳定的、机器可读的事件类型。
            task_id: 事件关联的任务标识符。
            payload: 可序列化为 JSON 的事件载荷（可含 ``_turn_id`` 键，会被提取为 turn_id）。

        返回:
            构造好的 RuntimeEvent。
        """

        payload = dict(payload)
        turn_id = payload.pop("_turn_id", None)
        tool_call_id = payload.get("tool_call_id")
        event = RuntimeEvent(
            event_type=event_type,
            task_id=task_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
            payload=payload,
        )
        self._logger.info(
            "runtime_event",
            extra={
                "msg": "runtime event recorded",
                "data": {
                    "task_id": task_id,
                    "event_type": str(event_type),
                    "event_id": event.event_id,
                },
            },
        )
        return event


def _langchain_messages_to_runtime(messages: list[BaseMessage]) -> list[RuntimeMessage]:
    """把 LangChain checkpoint 消息转换为模型无关的 ``RuntimeMessage`` 列表。

    仅用于把 checkpoint 轨迹落库为跨轮记忆；与 ``runtime_to_langchain`` 方向相反，但作为
    存储侧私有映射存在，不进入 ``core/llm/langchain_bridge`` 的公共桥接 API（避免凭空造
    反向转换）。

    参数:
        messages: LangGraph checkpoint 的 ``messages`` 通道内容。

    返回:
        可落库、模型无关的运行时消息列表。
    """

    runtime_messages: list[RuntimeMessage] = []
    for message in messages:
        role = _langchain_role(message)
        content_text = _extract_message_content(message.content)
        metadata: dict[str, str] = {}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            metadata["tool_calls"] = _safe_json(tool_calls)
        tool_call_id = getattr(message, "tool_call_id", None)
        if tool_call_id:
            metadata["tool_call_id"] = str(tool_call_id)
        runtime_messages.append(
            RuntimeMessage(role=role, content_text=content_text, metadata=metadata)
        )
    return runtime_messages


def _langchain_role(message: BaseMessage) -> str:
    """把 LangChain 消息类型映射为运行时 role 字符串。"""

    name = type(message).__name__
    if name == "HumanMessage":
        return "user"
    if name == "AIMessage":
        return "assistant"
    if name == "ToolMessage":
        return "tool"
    if name == "SystemMessage":
        return "system"
    return "user"


def _extract_message_content(content) -> str:
    """从 LangChain 消息 content 提取纯文本。"""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content)


def _safe_json(value) -> str:
    """把任意可序列化对象转为 JSON 字符串（失败则转 str）。"""

    import json

    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
