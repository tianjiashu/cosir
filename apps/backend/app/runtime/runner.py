"""协调任务生命周期与工作流执行。"""

import logging
from typing import AsyncIterator, Optional

from app.agents.profile import AgentProfile, default_developer_agent
from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.events.types import RuntimeEvent
from app.models.base import StreamingModelAdapter
from app.runtime.operations import RuntimeOperations
from app.storage.records import CheckpointRecord, TaskRecord
from app.tools.scheduler import ToolScheduler
from app.workflows.react_like import ReactLikeWorkflow
from app.workflows.types import AgentWorkflow


class AgentRuntime:
    """协调任务状态、模型流式产出、事件与日志。"""

    def __init__(
        self,
        settings: BackendSettings,
        task_store,
        context_builder: TextContextBuilder,
        model_adapter: StreamingModelAdapter,
        tool_scheduler: ToolScheduler,
        logger: logging.Logger,
        agent_profile: Optional[AgentProfile] = None,
        workflow: Optional[AgentWorkflow] = None,
    ) -> None:
        """初始化运行时依赖项。

        参数:
            settings: 控制运行时限制的后端配置。
            task_store: 用于任务状态与事件的存储实现。
            context_builder: 准备与模型无关的运行时消息的构建器。
            model_adapter: 用于模型调用的流式适配器。
            tool_scheduler: 用于校验和执行工具调用的调度器。
            logger: 用于可审计运行时记录的日志记录器。
            agent_profile: 作为任务执行主体的 Agent 档案。
            workflow: 可选的工作流策略。默认为 ReAct 风格工作流。

        返回:
            无。

        异常:
            无。

        副作用:
            在运行时实例上保存依赖项引用。
        """

        self._settings = settings
        self._task_store = task_store
        self._context_builder = context_builder
        self._model_adapter = model_adapter
        self._tool_scheduler = tool_scheduler
        self._logger = logger
        self._agent_profile = agent_profile or default_developer_agent()
        self._workflow = workflow or ReactLikeWorkflow()

    def create_task(self, input_text: str, session_id: Optional[str] = None) -> TaskRecord:
        """创建一个待执行的任务，留待后续执行。

        参数:
            input_text: 纯文本的用户任务。
            session_id: 可选的会话标识符。

        返回:
            已创建的任务记录。

        异常:
            ValueError: 如果 ``input_text`` 为空。

        副作用:
            在配置好的任务存储中持久化任务状态，并写入一条 info 日志。
        """

        task = self._task_store.create_task(
            input_text=input_text,
            session_id=session_id,
            agent_id=self._agent_profile.agent_id,
        )
        self._logger.info(
            "task_created task_id=%s session_id=%s agent_id=%s",
            task.task_id,
            session_id,
            self._agent_profile.agent_id,
        )
        return task

    def cancel_task(self, task_id: str) -> TaskRecord:
        """通过更新运行时状态来取消一个任务。

        参数:
            task_id: 需要取消的任务标识符。

        返回:
            已更新的任务记录。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            修改任务状态并写入一条可审计的日志记录。
        """

        task = self._task_store.update_status(task_id, "cancelled")
        self._record("run_cancelled", task_id, {"status": "cancelled"})
        self._logger.info("task_cancelled task_id=%s", task_id)
        return task

    async def run_task(self, task_id: str) -> AsyncIterator[RuntimeEvent]:
        """运行一个任务并流式产出运行时事件。

        参数:
            task_id: 待执行任务的标识符。

        生成:
            表示模型增量与运行状态变化的 RuntimeEvent 值。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            更新任务状态、持久化事件并写入运行时日志。
        """

        task = self._task_store.get_task(task_id)
        if task.status != "pending":
            existing_events = self._task_store.list_events(task.task_id)
            if existing_events:
                for event in existing_events:
                    yield event
                return
            yield self._record(
                "run_failed",
                task.task_id,
                {"status": task.status, "error": "task is not pending"},
            )
            return

        agent_profile = self._resolve_task_agent_profile(task)
        if agent_profile is None:
            self._task_store.update_status(task.task_id, "failed")
            self._logger.error(
                "agent_profile_unavailable task_id=%s task_agent_id=%s runtime_agent_id=%s",
                task.task_id,
                task.agent_id,
                self._agent_profile.agent_id,
            )
            yield self._record(
                "run_failed",
                task.task_id,
                {
                    "status": "failed",
                    "error": "agent_profile_unavailable",
                    "task_agent_id": task.agent_id,
                    "runtime_agent_id": self._agent_profile.agent_id,
                },
            )
            return

        self._task_store.update_status(task.task_id, "running")
        yield self._record(
            "run_started",
            task.task_id,
            {"status": "running", "agent": agent_profile.to_dict()},
        )

        operations = RuntimeOperations(
            settings=self._settings,
            task_store=self._task_store,
            context_builder=self._context_builder,
            model_adapter=self._model_adapter,
            tool_scheduler=self._tool_scheduler,
            logger=self._logger,
            agent_profile=agent_profile,
            record_event=self._record,
        )
        yield operations.create_checkpoint(task.task_id, "run_started")

        try:
            async for event in self._workflow.run(task, operations):
                yield event
            return
        except Exception as exc:
            self._task_store.update_status(task.task_id, "failed")
            self._task_store.close_running_steps_for_task(
                task.task_id,
                "failed",
                str(exc),
            )
            self._logger.exception("task_failed task_id=%s", task.task_id)
            yield operations.create_checkpoint(task.task_id, "run_failed")
            yield self._record(
                "run_failed",
                task.task_id,
                {"status": "failed", "error": str(exc)},
            )

    def list_events(self, task_id: str) -> list:
        """返回单个任务的已存储事件。

        参数:
            task_id: 需要返回其事件时间线的任务标识符。

        返回:
            为该任务存储的运行时事件列表。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        return self._task_store.list_events(task_id)

    def list_steps(self, task_id: str) -> list:
        """返回单个任务的已存储步骤。

        参数:
            task_id: 需要返回其步骤时间线的任务标识符。

        返回:
            为该任务存储的运行时步骤列表。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        return self._task_store.list_steps_for_task(task_id)

    def list_checkpoints(self, task_id: str) -> list:
        """返回单个任务的已存储检查点。

        参数:
            task_id: 需要返回其检查点的任务标识符。

        返回:
            为该任务持久化的检查点记录列表。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        checkpoints: list[CheckpointRecord] = self._task_store.list_checkpoints(task_id)
        return checkpoints

    def get_task(self, task_id: str) -> TaskRecord:
        """按标识符返回任务状态。

        参数:
            task_id: 需要获取的任务标识符。

        返回:
            匹配的任务记录。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        return self._task_store.get_task(task_id)

    def _resolve_task_agent_profile(self, task: TaskRecord) -> Optional[AgentProfile]:
        """返回被允许执行该任务的 Agent 档案。

        参数:
            task: 被选中进行执行的待执行任务。

        返回:
            当当前运行时 Agent 档案与任务持久化的 ``agent_id`` 匹配时返回该档案，
            否则返回 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        if task.agent_id == self._agent_profile.agent_id:
            return self._agent_profile
        return None

    def _record(self, event_type: str, task_id: str, payload: dict) -> RuntimeEvent:
        """创建、存储并记录一个运行时事件。

        参数:
            event_type: 稳定的事件类型字符串。
            task_id: 与该事件关联的任务标识符。
            payload: 可序列化为 JSON 的事件载荷。

        返回:
            已创建的运行时事件。

        异常:
            KeyError: 如果任务在存储中不存在。

        副作用:
            将事件追加到存储并写入一条 info 日志记录。
        """

        event = RuntimeEvent(event_type=event_type, task_id=task_id, payload=payload)
        self._task_store.append_event(event)
        self._logger.info("runtime_event task_id=%s type=%s", task_id, event_type)
        return event
