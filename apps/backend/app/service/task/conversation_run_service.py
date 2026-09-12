"""Conversation Run orchestration service.

单一职责：编排 run 的创建与管理——创建 run 时同步更新所属任务的最新 run ID 和消息预览。

职责边界：
- 负责：run 创建（含任务最新 run 更新）、run 查询与状态更新、pending run 的原子启动。
- 不负责：直接 SQL 操作（委托给 ``ConversationRunCrud``/``TaskCrud``）；Run 创建期的
  canonical 初始 user message 通过 Task context owner 写入。
"""

from uuid import uuid4

from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from app.assistant_transport.event import (
    RunInitializedEvent,
    RunStatusChangedEvent,
    UserInputAppendedEvent,
)
from app.config.logging.logger import log
from app.core.llm_provider.capability.provider_capability import ProviderCapability
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import ConversationRunRecord, ConversationRunStatus
from app.models.json_helpers import ConversationRunError
from app.service import depends as service_depends
from app.service.depends import get_provider_service
from app.service.task.conversation_task_context_service import ConversationTaskContextService
from app.storage.store_engines import main_session_factory
from app.utils.message_content import content_to_text


class ConversationRunService:
    """Orchestrate run creation, queries, status management, and execution start."""

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

    @staticmethod
    def _terminal_usage(
        usage_stats: ConversationRunUsageStats | None,
    ) -> dict[str, int | None]:
        """Return the exact six-key usage payload for every terminal Run."""

        return (usage_stats or ConversationRunUsageStats()).to_dict()

    @staticmethod
    def _terminal_error(
        status: ConversationRunStatus, end_reason: str | None
    ) -> ConversationRunError | None:
        """Map a terminal status to a short controlled persisted error contract."""

        if status is ConversationRunStatus.COMPLETED:
            return None
        code = end_reason if isinstance(end_reason, str) and end_reason.isidentifier() else None
        if code is None:
            code = "run_cancelled" if status is ConversationRunStatus.CANCELLED else "run_failed"
        return {
            "code": code,
            "message": "运行已取消" if status is ConversationRunStatus.CANCELLED else "运行失败",
            "retryable": False,
        }

    @staticmethod
    def _publish_post_commit_event(event: object, event_name: str) -> None:
        """Publish a post-commit transport event without hiding durable DB failures.

        Run/context persistence is the business fact boundary. Once its transaction has
        committed, a projector or subscriber failure is a degraded read-model/transport
        condition and must be logged rather than aborting the Agent execution. Callers must
        invoke this helper only after the owning database write has succeeded.
        """

        try:
            service_depends.get_conversation_event_projector().process(event)
        except Exception:
            log.exception(
                "conversation_post_commit_event_failed",
                extra={
                    "msg": "业务事实已提交，Transport 事件失败并被降级",
                    "data": {
                        "event_name": event_name,
                        "task_id": getattr(event, "task_id", None),
                        "run_id": getattr(event, "run_id", None),
                    },
                },
            )

    def have_run_in_runing(self, task_id: int, session: Session | None = None) -> bool:
        """检查任务是否正在运行中。

        参数:
            task_id: 任务标识。
            session: 可选，数据库会话；为 None 时从依赖获取。

        返回:
            如果任务正在运行中，返回 True；否则返回 False。

        异常:
            None。

        副作用:
            None。
        """
        statuses = (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value)
        if session is not None:
            return self._run.has_run_in_status(task_id, statuses, session)
        with self._session_factory() as owned_session:
            return self._run.has_run_in_status(task_id, statuses, owned_session)

    def create_run(
        self,
        task_id: int,
        input_text: str,
        agent_id: str | None = None,
        status: str = "pending",
        provider_id: int | None = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        session: Session | None = None,
    ) -> ConversationRunRecord:
        """Create a Conversation Run and initialize its canonical context.

        附件在创建阶段即完成处理：非图片附件（文件 / 目录 / 链接）渲染为文本前缀拼进
        ``input_text`` 落库，运行期模型用已有工具（read_file / list_directory /
        search_files / web_extract）按需读取；图片附件单独抽出为 ``image_paths``
        落库，运行期走多模态 block 通道（build_user_content_blocks）。视觉能力拦截
        按附件类型判定，模型不支持视觉时提前报错。

        参数:
            task_id: 所属任务标识。
            input_text: 本轮用户输入文本。
            status: 初始状态，默认 ``"pending"``。
            agent_id: 可选，本轮回绑定的 agent 标识；为 None 时回退到默认 ``"main_agent"``
                （与 Assistant Transport 主入口的默认值一致，非 ``"developer"``）。
            provider_id: 可选，模型归属厂商标识（指向 ``providers.id``）；None 表示未指定。
                与 ``model_name`` 配对出现：两者皆非 None 时按厂商能力校验模型；
                仅 ``model_name`` 非 None 而 ``provider_id`` 为 None 视为契约不完整，
                抛 ``ValueError``。
            model_name: 可选，本次请求的模型名（litellm 路由名）；None 表示用户
                未选择模型（前端优先校验、后端兜底报错）。
            reasoning_effort: 可选，思考努力等级（low/high/max）；None 表示用户未指定。
            session: 可选，由上层跨表事务传入的数据库会话。传入时本方法不提交事务，
                由调用方统一提交；未传入时保持独立创建事务的行为。

        返回:
            新创建的 ``ConversationRunRecord``（``input_text`` 已含附件文本前缀，``image_paths``
            仅含图片路径）。

        异常:
            ValueError: 如果 ``input_text`` 为空或全空白，或模型不在厂商能力范围内。
            VisionNotSupportedError: 如果携带图片附件但模型不支持视觉输入。
            sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

        副作用:
            将命令行更新为一次持久化 Conversation Run（``input_text`` 含附件前缀、
            ``image_paths`` 仅图片、``model_name`` 为解析后的最终模型名）；
            更新所属任务最新轮次信息；
            写入创建期附件构成日志。
        """
        # 厂商-模型契约校验：仅当两者皆非 None 时按厂商能力校验模型归属。
        # provider_id 为 None 但 model_name 已设，属契约不完整（前端应配对传入），
        # 显式抛 ValueError（由 API 层映射为 400），避免 get_provider(None) 误报 404。
        if model_name is not None and provider_id is None:
            raise ValueError(
                f"provider_id is required when model_name is set (model_name={model_name})"
            )
        if provider_id is not None:
            provider = get_provider_service().get_provider(provider_id)
            provider_capability = ProviderCapability.get_capability(provider.name)
            if model_name not in provider_capability.models:
                raise ValueError(
                    f"model_name {model_name} not in provider capability "
                    f"{provider_capability.models}"
                )
        context = getattr(self, "_context", None) or ConversationTaskContextService()

        def persist_facts(persist_session: Session | None) -> ConversationRunRecord:
            run = self._run.create(
                task_id,
                input_text,
                status,
                agent_id=agent_id,
                provider_id=provider_id,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                session=persist_session,
            )
            context.append_user_message_once(task_id, run.id, input_text, session=persist_session)
            self._task.set_current_run_id(task_id, run.id, session=persist_session)
            return run

        if session is not None:
            run = persist_facts(session)
        elif self._session_factory is not None:
            with self._session_factory.begin() as owned_session:
                run = persist_facts(owned_session)
        else:
            # Lightweight unit-test harnesses may deliberately omit storage setup.
            run = persist_facts(None)

        # With an external session, the caller owns the transaction and must publish only
        # after it commits. ConversationRunCommandService is that owner. Publishing here
        # would expose uncommitted facts and duplicate the owner's events.
        if session is None:
            self._publish_post_commit_event(
                RunInitializedEvent(task_id=task_id, run_id=run.id),
                "run_initialized",
            )
            self._publish_post_commit_event(
                UserInputAppendedEvent(task_id=task_id, run_id=run.id, text=input_text),
                "user_input_appended",
            )
        return run

    def get_run(self, run_id: int) -> ConversationRunRecord:
        return self._run.get(run_id)

    def list_runs_for_task(self, task_id: int) -> list[ConversationRunRecord]:
        return self._run.list_by_task(task_id)

    def list_recoverable(self) -> list[ConversationRunRecord]:
        """返回应用启动时可恢复的 pending/running 运行。"""
        return self._run.list_recoverable()

    def reset_run_for_edit(
        self,
        run_id: int,
        input_text: str,
        provider_id: int | None = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        session: Session | None = None,
    ) -> ConversationRunRecord | None:
        """原地重置一个已结束 run，替换输入并创建新的 checkpoint 身份。

        仅允许非 active run 编辑；调用方负责在同一 task 锁内清理并重建 context。
        传入 ``session`` 时复用外部事务且不自行提交。
        """

        if not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        allowed_statuses = (
            ConversationRunStatus.COMPLETED.value,
            ConversationRunStatus.FAILED.value,
            ConversationRunStatus.CANCELLED.value,
        )
        return self._run.reset_for_edit(
            run_id=run_id,
            input_text=input_text,
            checkpoint_thread_id=str(uuid4()),
            allowed_statuses=allowed_statuses,
            provider_id=provider_id,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            session=session,
        )

    def resume_cancelled_run(self, run_id: int) -> ConversationRunRecord | None:
        """恢复任意 cancelled run，并清理上一次执行的终态字段。"""

        record = self._run.resume_cancelled(run_id)
        if record is None:
            return None
        self._publish_post_commit_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.RUNNING,
            ),
            "run_running",
        )
        return record

    def recover_orphaned_runs(
        self, end_reason: str = "runtime_restarted"
    ) -> list[ConversationRunRecord]:
        """把当前进程启动前遗留的 pending/running run 收敛为 cancelled。

        Run 终态与未闭合工具结果在同一个数据库事务内收敛；事务提交后再投影 Run 状态。
        """

        recovered: list[ConversationRunRecord] = []
        for run in self.list_recoverable():
            session_factory = getattr(self, "_session_factory", None)
            context = getattr(self, "_context", None)
            publish_status = session_factory is not None and context is not None
            if session_factory is None or context is None:
                record = self._run.cancel_recoverable_for_restart(
                    run.id,
                    end_reason,
                    usage=self._terminal_usage(None),
                    error=self._terminal_error(ConversationRunStatus.CANCELLED, end_reason),
                )
            else:
                with session_factory.begin() as session:
                    record = self._run.cancel_recoverable_for_restart(
                        run.id,
                        end_reason,
                        usage=self._terminal_usage(None),
                        error=self._terminal_error(ConversationRunStatus.CANCELLED, end_reason),
                        session=session,
                    )
                    if record is not None:
                        context.recover_interrupted_run(run.task_id, run.id, session=session)
            if record is not None:
                recovered.append(record)
            if record is not None and publish_status:
                try:
                    service_depends.get_conversation_event_projector().process(
                        RunStatusChangedEvent(
                            task_id=record.task_id,
                            run_id=record.id,
                            status=ConversationRunStatus.CANCELLED,
                            end_reason=end_reason,
                            usage_stats=ConversationRunUsageStats(),
                        )
                    )
                except Exception:
                    log.exception(
                        "orphan_recovery_projector_failed",
                        extra={
                            "msg": "孤儿 Run 已提交，状态 projector 失败并被降级",
                            "data": {"task_id": record.task_id, "run_id": record.id},
                        },
                    )
        return recovered

    def complete_run_if_running(
        self,
        run_id: int,
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """Complete a running Conversation Run atomically.

        业务语义：仅 ``running`` 可进入 ``completed`` 终态并落库回复文本；约束收敛在
        本方法（service 层），CRUD 层只做通用的「状态白名单 + 原子更新」。Agent 对该
        轮次的最终回答文本通过 ``final_output`` 一并写入，供快速检索与审计。

        参数:
            run_id: 待完成的 Conversation Run 标识。
            final_output: 可选，Agent 对该轮次的最终回答文本；为 None 时不修改该列。
            usage_stats: 可选，运行用量统计，终态时按六键契约落库并发布事件。

        返回:
            成功完成时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 run 状态为 completed，可选的 final_output 列。
        """

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.COMPLETED.value,
            (ConversationRunStatus.RUNNING.value,),
            None,
            final_output=final_output,
            usage=self._terminal_usage(usage_stats),
            error=self._terminal_error(ConversationRunStatus.COMPLETED, None),
        )
        if record is None:
            return None
        self._publish_post_commit_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.COMPLETED,
                usage_stats=usage_stats,
            ),
            "run_completed",
        )
        return self._run.get(run_id)

    def complete_run_with_message(
        self,
        run_id: int,
        message: AIMessage,
        end_reason: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """将 Run 标记 completed，并发布其 Transport 展示状态。"""

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.COMPLETED.value,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            end_reason,
            final_output=content_to_text(message.content),
            usage=self._terminal_usage(usage_stats),
            error=self._terminal_error(ConversationRunStatus.COMPLETED, end_reason),
        )
        if record is None:
            return None
        self._publish_post_commit_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.COMPLETED,
                end_reason=end_reason,
                usage_stats=usage_stats,
            ),
            "run_completed",
        )
        return self._run.get(run_id)

    def fail_run_if_running(
        self,
        run_id: int,
        end_reason: str | None = None,
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """Fail a running Conversation Run atomically.

        业务语义：仅 ``running`` 可进入 ``failed`` 终态；约束收敛在本方法（service 层），
        CRUD 层只做通用的「状态白名单 + 原子更新」。终态同时写入 ``final_output``，使复用
        同一工作流的子 Agent 即便失败，主 Agent 也能从委派结果中感知其终态输出。

        参数:
            run_id: 待失败落定的 Conversation Run 标识。
            end_reason: 可选失败原因。
            final_output: 可选，随终态一并写入的失败说明文本，供委派场景主 Agent 感知。
            usage_stats: 可选，运行用量统计，终态时按六键契约落库并发布事件。

        返回:
            成功失败落定时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 run 状态为 failed（并可选写入 end_reason 与 final_output），并发布
            状态变更事件。
        """

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.FAILED.value,
            (ConversationRunStatus.RUNNING.value,),
            end_reason,
            final_output=final_output,
            usage=self._terminal_usage(usage_stats),
            error=self._terminal_error(ConversationRunStatus.FAILED, end_reason),
        )
        if record is None:
            return None
        self._publish_post_commit_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.FAILED,
                end_reason=end_reason,
                usage_stats=usage_stats,
            ),
            "run_failed",
        )
        return self._run.get(run_id)

    def fail_run_if_pending_or_running(
        self,
        run_id: int,
        end_reason: str | None = None,
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """将尚未启动或正在执行的 Conversation Run 原子落定为 failed。

        参数:
            run_id: 待失败落定的 Conversation Run 标识。
            end_reason: 可选失败原因。
            final_output: 可选，随终态一并写入的失败说明文本，供委派场景主 Agent 感知。
        """
        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.FAILED.value,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            end_reason,
            final_output=final_output,
            usage=self._terminal_usage(usage_stats),
            error=self._terminal_error(ConversationRunStatus.FAILED, end_reason),
        )
        if record is None:
            return None
        self._publish_post_commit_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.FAILED,
                end_reason=end_reason,
                usage_stats=usage_stats,
            ),
            "run_failed",
        )
        return self._run.get(run_id)

    def cancel_run_if_running(
        self,
        run_id: int,
        end_reason: str = "user_cancelled",
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """将 active Run 标记 cancelled，并发布其 Transport 展示状态。

        终态同时写入 ``final_output``，使复用同一工作流的子 Agent 即便被取消，主 Agent
        也能从委派结果中感知其已产出（或被中断）的内容，而非仅看到一个空终态。

        参数:
            run_id: 待取消的 Conversation Run 标识。
            end_reason: 稳定的取消原因。
            final_output: 可选，随终态一并写入的取消说明/部分输出文本，供委派场景主 Agent 感知。
            usage_stats: 可选，运行用量统计，终态时按六键契约落库并发布事件。
        """

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.CANCELLED.value,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            end_reason,
            final_output=final_output,
            usage=self._terminal_usage(usage_stats),
            error=self._terminal_error(ConversationRunStatus.CANCELLED, end_reason),
        )
        if record is None:
            return None
        self._publish_post_commit_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.CANCELLED,
                end_reason=end_reason,
                usage_stats=usage_stats,
            ),
            "run_cancelled",
        )
        return self._run.get(run_id)

    def claim_pending_run(self, run_id: int) -> bool:
        """以条件更新方式将 pending Conversation Run 标记为 running。

        业务语义：仅 ``pending`` 可抢占为 ``running``，该约束收敛在本方法（service 层），
        CRUD 层只做通用的「状态白名单 + 原子更新」。返回 ``bool`` 表示本次是否成功抢占，
            供上层（runner / delegation executor）判断「是否由我执行该 run」。

        参数:
            run_id: 待抢占的 Conversation Run 标识。

        返回:
            抢占成功（本次确实把 pending 更新为 running）返回 True；run 已被其他执行者抢占或
            非 pending 状态返回 False。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 run 状态为 running，并发布 running 状态变更事件（以数据库更新成功为
            幂等闸门，重复认领不会重复发布）。
        """
        row = self._run.update_status_if_in(
            run_id=run_id,
            target_status=ConversationRunStatus.RUNNING.value,
            allowed_statuses=(ConversationRunStatus.PENDING.value,),
        )
        if row is not None:
            service_depends.get_conversation_event_projector().process(
                RunStatusChangedEvent(
                    task_id=row.task_id,
                    run_id=run_id,
                    status=ConversationRunStatus.RUNNING,
                )
            )
        return row is not None

    def claim_or_resume_run(self, run_id: int) -> bool:
        """认领一个待执行或后端重启后遗留的 Conversation Run。

        ``pending`` 通过原子状态迁移进入 ``running``；``running`` 表示旧进程在
        持久化层已经认领过，但进程内执行器已丢失，恢复入口可以继续驱动同一个 run。
        任意 ``cancelled`` run 都允许恢复；``end_reason`` 只用于展示与审计，不参与资格判断。
        调用方必须先持有 task 级运行锁，避免同一进程重复启动恢复执行。

        参数:
            run_id: 待认领的运行标识。

        返回:
            当前调用方可以继续执行返回 True；run 已进入终态或已被当前进程之外的执行
            占用返回 False。

        异常:
            KeyError: run 不存在。

        副作用:
            pending run 成功迁移时发布一次 running 状态事件；running run 不重复发布。
        """

        row = self._run.update_status_if_in(
            run_id=run_id,
            target_status=ConversationRunStatus.RUNNING.value,
            allowed_statuses=(ConversationRunStatus.PENDING.value,),
        )
        if row is not None:
            service_depends.get_conversation_event_projector().process(
                RunStatusChangedEvent(
                    task_id=row.task_id,
                    run_id=run_id,
                    status=ConversationRunStatus.RUNNING,
                )
            )
            return True

        resumed = self.resume_cancelled_run(run_id)
        if resumed is not None:
            return True

        current = self._run.get(run_id)
        return current.status == ConversationRunStatus.RUNNING.value
