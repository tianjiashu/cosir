"""Run 创建（按调度方选定的发起配置）与启动恢复用例；输入解析、编辑移出与队列不归这里。"""

from app.config.logging.logger import log
from app.core.context.tool_call_closure import ContextProtocolError
from app.core.events import RunInitializedEvent
from app.models import (
    ConversationRunRecord,
    ConversationRunStatus,
)
from app.models.conversation_run_progress import ConversationRunProgress
from app.models.enums.conversation_run_status import RunTransition
from app.models.task_message import RunLaunch
from app.service import depends as service_depends
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
        self._run_state = service_depends.get_conversation_run_state_service()
        self._context = ConversationTaskContextService()
        self._session_factory = main_session_factory()

    def create_run(self, task_id: int, launch: RunLaunch) -> ConversationRunRecord:
        """新建 Run 的唯一路径：同一事务内按发起配置建 running Run → 写 Task 当前 Run 指针。

        配置由调度方按 ``dispatch_policy.next_action`` 从队列选定，这里不读队列；输入仍在外部
        队列，由新执行的初始投递交给 ``prepare_input`` 并入。只由 Task 的调度 actor（``TaskActor``）
        调用并随即交给执行器，因此直接以 running 创建。
        """
        config = get_model_config_service().get_config(launch.model_config_id)
        with self._session_factory.begin() as session:
            run = self._run.create(
                task_id,
                ConversationRunStatus.RUNNING.value,
                agent_id=launch.agent_id,
                model_config_id=launch.model_config_id,
                context_window_total=config.context_window_k * 1000,
                options=launch.options,
                session=session,
            )
            self._task.set_current_run_id(task_id, run.id, session=session)
        service_depends.get_conversation_event_projector().process(
            RunInitializedEvent(
                task_id=task_id,
                run_id=run.id,
                context_window_total=run.context_window_total,
                model_config_id=run.model_config_id,
                options=run.options,
            ),
        )
        return run

    def recover_orphaned_runs(
        self, end_reason: str = "runtime_restarted"
    ) -> list[ConversationRunRecord]:
        """启动时取消失去执行者的 running Run；suspended 没有执行者、不需要收敛，保留等消息唤醒。
        每个 Run 的状态与工具占位同事务提交；inbox 保留，不自动继续，不发布尚无订阅者的事件。
        配对依据不完整时不补占位，记录错误并提交取消，继续恢复其他 Run。"""

        recovered: list[ConversationRunRecord] = []
        for run in self._run.list_recoverable():
            repaired_tool_call_ids: list[str] = []
            with self._session_factory.begin() as session:
                record = self._run_state.transition(
                    run.id,
                    RunTransition.CANCEL,
                    end_reason=end_reason,
                    execution_data=ConversationRunProgress(
                        step_count=run.execution_data.step_count,
                        tool_error_count=run.execution_data.tool_error_count,
                    ),
                    session=session,
                )
                if record is not None:
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
