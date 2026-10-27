"""启动恢复用例：把重启后失去执行者的遗留 Run 收敛为终态并补齐工具占位。"""

from app.config.logging.logger import log
from app.core.context.tool_call_closure import ContextProtocolError
from app.models import (
    ConversationRunRecord,
    ConversationRunStatus,
)
from app.models.enums.conversation_run_status import RunTransition
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory


class RecoverService:
    """后端启动期的 Run 恢复：只收敛遗留 running Run，不动 suspended 与 inbox。"""

    def __init__(self) -> None:
        """绑定恢复所需的 Run CRUD、状态迁移、Task context 与事务工厂。"""

        self._run = service_depends.get_conversation_run_crud()
        self._ledger = service_depends.get_conversation_run_ledger()
        self._context = service_depends.get_conversation_task_context_service()
        self._session_factory = main_session_factory()

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
                record = self._ledger.transition(
                    run.id,
                    RunTransition.CANCEL,
                    end_reason=end_reason,
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
