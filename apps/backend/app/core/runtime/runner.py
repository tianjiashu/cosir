"""Coordinate task lifecycle and workflow execution."""

import asyncio
import dataclasses
import os
from collections.abc import AsyncGenerator
from uuid import uuid4

from langchain_core.messages import BaseMessage

from app.config.logging import (
    trace_log_extra,
)
from app.config.logging.logger import log
from app.config.settings import BackendSettings
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.context import TextContextBuilder
from app.core.runtime.runs.checkpointer import build_checkpointer
from app.core.runtime.runtime_operations import RuntimeOperations
from app.models import TurnRecord
from app.models.enums.event_type import EventType
from app.models.payload import RunCancelledPayload, RunFailedPayload, RunStartedPayload
from app.models.payload.runtime_event_payload import RuntimeEventPayload
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
    - 负责：任务执行编排、运行时事件（含持久化到 ``runtime_events`` 表以供回放）、取消。
    - 不负责：checkpoint 回放与历史事件回看（历史对话由 ``GET /tasks/{task_id}/turns``
      提供，细粒度事件 timeline 由 ``runtime_events`` 表 + 回放端点提供）、工作区 / 任务 /
      轮次的 CRUD 与查询（委托给对应 service 层）；不对外暴露 service 访问器，
      service 仅作为本引擎的私有协作者。
    """

    def __init__(
        self,
        settings: BackendSettings,
        task_service: TaskService,
        turn_service: TurnService,
        context_builder: TextContextBuilder,
        tool_scheduler: ToolScheduler,
        agent_registry: AgentProfileRegistry,
        model_tools: list[ToolDefinition] | None = None,
    ) -> None:
        """Initialize the execution engine with its private collaborators.

        参数:
            settings: 后端运行配置。
            task_service: 任务编排服务（私有协作者，不对外暴露）。
            turn_service: 轮次编排服务（私有协作者，不对外暴露）。
            context_builder: 文本上下文构建器。
            tool_scheduler: 工具调度器。
            agent_registry: 进程级 agent profile 目录；引擎按 ``agent_id`` 从中解析
                本次执行由哪个 profile 驱动，自身不再绑定单一 agent。
            model_tools: 暴露给模型的工具定义列表。
        """

        self._settings = settings
        self._task_service = task_service
        self._turn_service = turn_service
        self._context_builder = context_builder
        self._tool_scheduler = tool_scheduler
        self._agent_registry = agent_registry
        self._model_tools = list(model_tools or [])

    @property
    def agent_registry(self) -> AgentProfileRegistry:
        """返回驱动本引擎的进程级 agent profile 目录（只读）。

        参数:
            无。

        返回:
            注入的 ``AgentProfileRegistry`` 实例。

        异常:
            无。

        副作用:
            无。
        """

        return self._agent_registry

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
            RunCancelledPayload(status="cancelled"),
            turn_id=turn_id,
        )
        log.info(
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
    ) -> AsyncGenerator[RuntimeEvent, None]:
        """执行单个 pending 轮次并实时流式产出运行时事件。

        只负责「pending → 认领 → 执行 → 流式事件」。逐条事件在 ``yield`` 前经 ``emit``
        持久化到 ``runtime_events`` 表（带本轮自增 sequence），供刷新 / 重连后通过回放端点
        重建细粒度 timeline；历史对话列表由 ``GET /tasks/{task_id}/turns`` 提供。SSE 通过
        断开兜底（finally 标记 failed）判定本轮是否中断。非 pending 轮次不应进入本方法，
        调用方（API 层）应先做 409 守卫；此处仅做防御性早退。

        参数:
            turn_id: 需要运行的轮次标识符。
            turn: 可选的预取轮次记录；缺省时按 ``turn_id`` 读取。
        """

        # 预取轮次记录
        if turn is None:
            turn = self._turn_service.get_turn(turn_id)
        task_id = turn.task_id
        # 预取任务记录
        task = self._task_service.get_task(task_id)

        # 本轮事件持久化的自增序号：``runtime_events`` 主键为 (turn_id, sequence)，
        # 必须为每条事件分配唯一递增序号，否则同 turn 多事件主键冲突、后者静默丢失。
        seq = 0

        async def emit(event: RuntimeEvent) -> RuntimeEvent:
            """落库并透传一条运行时事件（赋唯一递增 sequence）。

            在 ``yield`` 前调用：把事件以独立线程写入 ``runtime_events`` 表（避免阻塞
            SSE 事件循环），并为冻结的 ``RuntimeEvent`` 赋上本轮自增序号；持久化失败仅
            由 ``RuntimeEventCrud.save_event`` 内部记日志，不会中断流式运行。

            参数:
                event: 待落库并透传的运行时事件。

            返回:
                已赋序号、可直接 ``yield`` 的事件（与原事件 payload 一致）。

            异常:
                不向上抛出：落库异常被 ``save_event`` 内部吞掉并记日志。

            副作用:
                向 ``runtime_events`` 表插入一行（失败仅记日志）；推进本轮 ``seq`` 计数器。
            """

            nonlocal seq
            from app.storage.crud.runtime_event_crud import RuntimeEventCrud

            stamped = dataclasses.replace(event, sequence=seq)
            seq += 1
            await asyncio.to_thread(RuntimeEventCrud().save_event, stamped.to_dict())
            return stamped

        # 非 pending 轮次不应进入本方法，调用方（API 层）应先做 409 守卫；此处仅做防御性早退。
        if turn.status != "pending":
            log.warning(
                "run_turn_non_pending",
                extra={
                    "msg": "run_turn called for non-pending turn; refusing to execute",
                    "data": {"turn_id": turn_id, "status": turn.status},
                },
            )
            return

        # 解析本次执行的 agent profile：优先使用轮次创建时绑定的 agent_id，
        # 未绑定时回退到 task.agent_id 默认归属。
        requested_agent_id = turn.agent_id or task.agent_id
        agent_profile = self._resolve_agent_profile(requested_agent_id)
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
                        "requested_agent_id": requested_agent_id,
                        "task_agent_id": task.agent_id,
                    },
                ),
            )
            yield await emit(
                self._record(
                    EventType.RUN_FAILED,
                    task_id,
                    RunFailedPayload(
                        status="failed",
                        error="agent_profile_unavailable",
                        requested_agent_id=requested_agent_id,
                        task_agent_id=task.agent_id,
                    ),
                    turn_id=turn.turn_id,
                )
            )
            return

        if not self._turn_service.claim_pending_turn(turn.turn_id):
            # 已被其它连接抢占（极小概率的竞态）：本轮不再重复驱动，直接退出。
            # 关键：未成功认领即在进入下方 try/finally 之前 return，断开兜底只由真正
            # 持有本轮的连接负责，避免落败连接误标他连接正在驱动的 running turn。
            log.warning(
                "turn_claim_lost",
                extra={
                    "msg": "turn already claimed by another connection",
                    "data": {"turn_id": turn.turn_id},
                },
            )
            return

        # 自此本连接已持有本轮认领：try/finally 覆盖 RUN_STARTED 之后的全部路径，
        # 确保无论正常完成、异常逃逸还是客户端断开（GeneratorExit），终态都只由本连接决定。
        try:
            yield await emit(
                self._record(
                    EventType.RUN_STARTED,
                    task_id,
                    RunStartedPayload(status="running", agent_id=agent_profile.agent_id),
                    turn_id=turn.turn_id,
                )
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

            async for event in agent_profile.workflow.run(task, operations):
                yield await emit(event)
            await self._persist_turn_trajectory(turn.turn_id)
            return
        except Exception as exc:
            self._turn_service.update_turn_status(turn.turn_id, "failed", end_reason=str(exc))
            log.exception(
                "task_failed",
                extra={
                    "msg": "task execution failed",
                    "data": {"task_id": task_id},
                },
            )
            yield await emit(
                RuntimeEvent(
                    event_type=EventType.RUN_FAILED,
                    task_id=task_id,
                    turn_id=turn.turn_id,
                    payload=RunFailedPayload(status="failed", error=str(exc)),
                )
            )
        finally:
            # 本连接持有的清理收口：仅当本轮仍卡在 running（客户端断开导致运行被中止、
            # 或落终态前异常逃逸）时置 failed；正常完成 / 已失败 / 已取消均为幂等空操作。
            self._mark_turn_disconnected_if_running(turn.turn_id)

    def _mark_turn_disconnected_if_running(self, turn_id: str) -> None:
        """本连接持有的轮次若仍处于 ``running``，则落定为断开失败。

        仅在本引擎成功认领（claim）本轮后进入的清理路径（``run_turn`` 的 finally）中调用：
        正常完成 / 失败 / 取消时 turn 已落终态（非 ``running``），此处为幂等空操作；仅当
        客户端断开导致运行被中止、或落终态前异常逃逸使 turn 卡在 ``running`` 时，才置为
        ``failed``（``end_reason="client_disconnected"``），避免孤儿 ``running``。由于只有
        持有认领的连接才会走到这里，不会误标他连接正在驱动的 ``running`` turn。

        参数:
            turn_id: 待检查并可能落终态的轮次标识。

        返回:
            无。

        异常:
            不向上抛出：轮次可能已被清理，内部窄异常保护，避免 teardown 抛异常掩盖主流程结果。

        副作用:
            可能把 turn 状态由 ``running`` 置为 ``failed`` 并写日志。
        """

        try:
            if not self._turn_service.has_turn_status(turn_id, "running"):
                return
            self._turn_service.update_turn_status(
                turn_id, "failed", end_reason="client_disconnected"
            )
            log.info(
                "turn_marked_disconnected",
                extra={
                    "msg": f"客户端断开，轮次已标记为 failed，turn_id={turn_id}",
                    "data": {"turn_id": turn_id, "end_reason": "client_disconnected"},
                },
            )
        except Exception:
            log.exception(
                "turn_disconnect_mark_failed",
                extra={
                    "msg": f"客户端断开后标记轮次 failed 失败，turn_id={turn_id}",
                    "data": {"turn_id": turn_id},
                },
            )

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
                snapshot = await checkpointer.aget_tuple({"configurable": {"thread_id": turn_id}})
            if snapshot is None or not snapshot.checkpoint.get("channel_values"):
                return
            messages = snapshot.checkpoint["channel_values"].get("messages") or []
            runtime_messages = _langchain_messages_to_runtime(messages)
            self._turn_service.save_turn_messages(turn_id, runtime_messages)
        except Exception:
            log.exception(
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

    def _resolve_agent_profile(self, agent_id: str) -> AgentProfile | None:
        """按 ``agent_id`` 从目录解析出本次执行使用的 agent profile。

        引擎不再绑定单一 agent：每次执行都通过 ``agent_id`` 在 ``AgentProfileRegistry``
        中查找对应的 profile。未命中（目录中不存在该 id）返回 ``None``，由调用方走
        ``agent_profile_unavailable`` 失败分支。

        参数:
            agent_id: 待解析的 agent 标识（通常来自请求覆盖或 ``task.agent_id`` 默认归属）。

        返回:
            命中时返回对应的 ``AgentProfile``；未命中返回 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        return self._agent_registry.resolve(agent_id)

    def _record(
        self,
        event_type: EventType,
        task_id: str,
        payload: RuntimeEventPayload,
        turn_id: str | None = None,
    ) -> RuntimeEvent:
        """创建一条运行时事件。

        参数:
            event_type: 稳定的、机器可读的事件类型。
            task_id: 事件关联的任务标识符。
            payload: 与 ``event_type`` 匹配的 payload 实体。
            turn_id: 事件关联的轮次标识符。

        返回:
            构造好的 RuntimeEvent。
        """

        tool_call_id = getattr(payload, "tool_call_id", None)
        event = RuntimeEvent(
            event_type=event_type,
            task_id=task_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
            payload=payload,
        )
        log.info(
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
