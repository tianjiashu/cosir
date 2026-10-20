"""Run 创建、队列交接与启动恢复用例；用户输入解析由独立输入边界负责。"""

from sqlalchemy.orm import Session

from app.assistant_transport.event import RunInitializedEvent
from app.config.logging.logger import log
from app.core.context.tool_call_closure import ContextProtocolError
from app.models import (
    ConversationRunCommand,
    ConversationRunRecord,
    ConversationRunStatus,
)
from app.models.conversation_input import QueuedTaskInput
from app.models.conversation_run_options import ReasoningEffort
from app.models.conversation_run_progress import ConversationRunProgress
from app.service import depends as service_depends
from app.service.conversation_run.conversation_input_preparation_service import (
    ConversationInputPreparationService,
)
from app.service.conversation_run.conversation_run_state_service import ConversationRunStateService
from app.service.conversation_run.conversation_task_context_service import (
    ConversationTaskContextService,
)
from app.service.depends import get_model_config_service
from app.storage.store_engines import main_session_factory


class ConversationRunService:
    """创建和查询 Run，启动恢复时收敛遗留执行；消息解析不属于 Run。"""

    def __init__(self) -> None:
        """初始化轮次 service。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 CRUD 单例并保存引用。
        """

        self._task = service_depends.get_task_crud()
        self._run = service_depends.get_conversation_run_crud()
        self._context = ConversationTaskContextService()
        self._session_factory = main_session_factory()

    def create_run(
        self,
        task_id: int,
        agent_id: str | None = None,
        status: str = "pending",
        model_config_id: int | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        session: Session | None = None,
        run_command: ConversationRunCommand | None = None,
        queued_input: QueuedTaskInput | None = None,
    ) -> ConversationRunRecord:
        """同一事务创建 Run、初始 inbox 输入和 Task 当前 Run 指针。
        队列交接只创建 Run，不再次插入已存在的输入；调用方有外部事务时负责提交后投影。"""
        if queued_input is not None:
            payload = queued_input
        elif run_command is not None and model_config_id is not None:
            payload = ConversationInputPreparationService().prepare_input(
                task_id, run_command, model_config_id, reasoning_effort
            )
        else:
            raise ValueError("创建 Run 必须提供输入命令和模型，或已入队的输入")
        model_config_id = payload.model_config_id
        config = get_model_config_service().get_config(model_config_id)
        context_window_total = config.context_window_k * 1000

        def persist_facts(persist_session: Session) -> ConversationRunRecord:
            run = self._run.create(
                task_id,
                status,
                agent_id=agent_id,
                model_config_id=model_config_id,
                context_window_total=context_window_total,
                options=payload.options,
                session=persist_session,
            )
            from app.storage.crud.task_input_crud import TaskInputCrud

            if queued_input is None:
                TaskInputCrud().append(persist_session, task_id, payload)
            self._task.set_current_run_id(
                task_id,
                run.id,
                session=persist_session,
            )
            return run

        if session is not None:
            run = persist_facts(session)
        else:
            with self._session_factory.begin() as owned_session:
                run = persist_facts(owned_session)

        # 外部事务由调用方提交并投影，避免提前暴露未提交事实或重复发布。
        if session is None:
            service_depends.get_conversation_event_projector().process(
                RunInitializedEvent(
                    task_id=task_id,
                    run_id=run.id,
                    context_window_total=context_window_total,
                ),
            )
        return run

    def recover_orphaned_runs(
        self, end_reason: str = "runtime_restarted"
    ) -> list[ConversationRunRecord]:
        """启动时取消失去执行者的 pending/running Run；waiting 保留供用户决定。
        每个 Run 的状态与工具占位同事务提交；inbox 保留，不自动继续，不发布尚无订阅者的事件。
        配对依据不完整时不补占位，记录错误并提交取消，继续恢复其他 Run。"""

        recovered: list[ConversationRunRecord] = []
        for run in self._run.list_recoverable():
            repaired_tool_call_ids: list[str] = []
            with self._session_factory.begin() as session:
                record = self._run.update_status_if_in(
                    run_id=run.id,
                    target_status=ConversationRunStatus.CANCELLED.value,
                    allowed_statuses=(
                        ConversationRunStatus.PENDING.value,
                        ConversationRunStatus.RUNNING.value,
                    ),
                    end_reason=end_reason,
                    execution_data=ConversationRunProgress(
                        step_count=run.execution_data.step_count,
                        tool_error_count=run.execution_data.tool_error_count,
                    ),
                    usage=None,
                    error=ConversationRunStateService.terminal_error(
                        ConversationRunStatus.CANCELLED, end_reason
                    ),
                    session=session,
                )
                if record is not None:
                    self._context.invalidate_waiting_calls(run.task_id, run.id, session)
                    try:
                        repaired_tool_call_ids = self._context.close_unclosed_tool_calls_for_run(
                            run.task_id, run.id, session=session
                        )
                    except ContextProtocolError as error:
                        # 配对计划在任何补写前失败；保留坏 context，仍提交取消并恢复其他 Run。
                        log.error(
                            "run_recovery_context_protocol_invalid",
                            extra={
                                "msg": "上下文配对依据不完整，保留原记录并取消遗留 Run",
                                "data": {
                                    "task_id": run.task_id,
                                    "run_id": run.id,
                                    **error.log_data(),
                                },
                            },
                        )
            if record is None:
                continue
            recovered.append(record)
            log.info(
                "run_status_persisted",
                extra={
                    "msg": "遗留 Run 已收敛为取消",
                    "data": {
                        "task_id": run.task_id,
                        "run_id": record.id,
                        "status": ConversationRunStatus.CANCELLED.value,
                        "end_reason": end_reason,
                        "repaired_tool_call_count": len(repaired_tool_call_ids),
                    },
                },
            )
            for tool_call_id in repaired_tool_call_ids:
                log.info(
                    "tool_observation_persisted",
                    extra={
                        "msg": "orphan Run 的未闭合工具调用已补取消占位",
                        "data": {
                            "task_id": run.task_id,
                            "run_id": record.id,
                            "tool_call_id": tool_call_id,
                            "status": ConversationRunStatus.CANCELLED.value,
                        },
                    },
                )
        return recovered

    def save_execution(self, run_id: int, data: ConversationRunProgress) -> None:
        """保存恢复必要进度，不创建第二套生命周期状态。"""
        self._run.save_execution(run_id, data)

    def get_run(self, run_id: int) -> ConversationRunRecord:
        """按标识读取单个 Run 记录。

        参数:
            run_id: Conversation Run 标识。

        返回:
            对应的 ``ConversationRunRecord``。

        异常:
            KeyError: run 不存在。

        副作用:
            无。
        """

        return self._run.get(run_id)
