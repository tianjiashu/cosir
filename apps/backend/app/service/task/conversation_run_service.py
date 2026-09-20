"""Conversation Run 创建与编辑的用例编排。

单一职责：把领域命令编排成一次 Run 创建或原地编辑——解析输入与附件、在事务内落库 run 与
命令、更新所属 task 的最新 run，并按事务所有权决定是否发布 Transport 事件；另提供启动期的
遗留 active run 收口。

职责边界：
- 负责：``create_run`` / ``reset_run_for_edit`` 的用例编排、``recover_orphaned_runs`` 的
  崩溃恢复收口（Run 终态与未闭合工具调用在同一事务内写入）、命令输入与附件解析。
- 不负责：Run 状态迁移与查询（见 ``ConversationRunStateService``）；直接 SQL 操作
  （委托给 ``ConversationRunCrud``/``TaskCrud``）；Run 创建期的 canonical 初始 user
  message 通过 Task context owner 写入。
"""
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.assistant_transport.event import RunInitializedEvent
from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.llm_provider.capability.model_capability import ModelCapability
from app.core.llm_provider.capability.provider_capability import ProviderCapability
from app.models import (
    ConversationRunAttachmentInput,
    ConversationRunCommand,
    ConversationRunExtra,
    ConversationRunFileAttachment,
    ConversationRunRecord,
    ConversationRunStatus,
)
from app.service import depends as service_depends
from app.service.depends import get_provider_service
from app.service.task.conversation_run_state_service import terminal_error
from app.service.task.conversation_task_context_service import ConversationTaskContextService
from app.storage.store_engines import main_session_factory


@dataclass(frozen=True, slots=True)
class _PreparedConversationRunInput:
    """Run command 经过领域校验和附件解析后的持久化输入。"""

    input_text: str
    image_paths: list[str]
    extra: ConversationRunExtra | None


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

    def _prepare_command(
        self,
        task_id: int,
        command: ConversationRunCommand,
        *,
        run_id: int | None,
        model_name: str | None,
    ) -> _PreparedConversationRunInput:
        """把领域输入命令解析为 Run 持久化所需的最终事实。

        普通附件 token 只保留在 ``ConversationRunExtra.display_text``；传给模型的
        ``input_text`` 则把 token 替换为已经校验存在的本机路径。图片顺序 marker 同样
        只存在于既有 ``display_text`` JSON 值中，并在模型输入边界移除。编辑命令可以从旧
        Run 恢复请求中省略的附件路径。图片附件在这里 finalize 为 workspace-relative
        路径，避免 ``ConversationRunCommandService`` 和其它入口重复实现该规则。
        """

        file_attachments = self._resolve_file_attachments(
            command.attachments,
            run_id=run_id,
            display_text=command.display_text,
        )
        input_text = self._resolve_file_tokens(command.display_text, file_attachments)

        if command.image_asset_ids:
            if not model_name:
                raise ValueError("model_name is required when image attachments are present")
            if not ModelCapability.get_capability(model_name).supports_image:
                raise ValueError("model does not support image")
        image_paths = self._finalize_image_assets(
            task_id,
            command.image_asset_ids,
            model_name,
        )
        if not input_text.strip() and not image_paths:
            raise ValueError("input_text must be a non-empty string")

        return _PreparedConversationRunInput(
            input_text=input_text,
            image_paths=image_paths,
            extra=(
                ConversationRunExtra(
                    display_text=command.display_text,
                    attachments=file_attachments,
                )
                if file_attachments or image_paths
                else None
            ),
        )

    def _resolve_file_attachments(
        self,
        requested: list[ConversationRunAttachmentInput],
        *,
        run_id: int | None,
        display_text: str,
    ) -> list[ConversationRunFileAttachment]:
        """解析普通附件 token，并从既有 Run 恢复编辑请求缺失的路径。"""

        existing_by_id: dict[str, ConversationRunFileAttachment] = {}
        if run_id is not None:
            try:
                existing_run = self._run.get(run_id)
                if existing_run.extra is not None:
                    existing_by_id = {
                        attachment["id"]: {
                            "id": attachment["id"],
                            "name": attachment["name"],
                            "content_type": attachment["content_type"],
                            "path": attachment["path"],
                        }
                        for attachment in existing_run.extra.attachments
                    }
            except KeyError:
                pass

        merged: dict[str, ConversationRunFileAttachment] = dict(existing_by_id)
        for attachment in requested:
            if not attachment.id or attachment.path is None:
                continue
            candidate = Path(attachment.path)
            if not candidate.is_file():
                raise ValueError("ordinary file attachment is unavailable")
            merged[attachment.id] = {
                "id": attachment.id,
                "name": attachment.name,
                "content_type": attachment.content_type,
                "path": attachment.path,
            }

        result: list[ConversationRunFileAttachment] = []
        for attachment_id in dict.fromkeys(Constant.Cosir.LOCAL_FILE_TOKEN.findall(display_text)):
            resolved_attachment = merged.get(attachment_id)
            if resolved_attachment is None or not Path(resolved_attachment["path"]).is_file():
                raise ValueError("ordinary file attachment is unavailable")
            result.append(resolved_attachment)
        return result

    @staticmethod
    def _resolve_file_tokens(
        text: str,
        attachments: list[ConversationRunFileAttachment],
    ) -> str:
        """将用户可见文本中的普通文件 token 替换为模型可读的本机路径。"""

        by_id = {attachment["id"]: attachment for attachment in attachments}

        def replace(match: re.Match[str]) -> str:
            attachment = by_id.get(match.group(1))
            if attachment is None:
                raise ValueError("ordinary file attachment is unavailable")
            return attachment["path"]

        return Constant.Cosir.LOCAL_IMAGE_TOKEN.sub(
            "", Constant.Cosir.LOCAL_FILE_TOKEN.sub(replace, text)
        )

    @staticmethod
    def _finalize_image_assets(
        task_id: int,
        image_asset_ids: list[str],
        model_name: str | None,
    ) -> list[str]:
        """把图片附件 id finalize 为模型使用的 workspace-relative 路径。"""

        if not image_asset_ids:
            return []
        from app.service.attachment.attachment_service import AttachmentService

        attachment_service = AttachmentService()
        finalized = [
            attachment_service.finalize(task_id, asset_id, model_name or "")
            for asset_id in image_asset_ids
        ]
        return [attachment_service.relative_path(task_id, result.path) for result in finalized]

    def create_run(
        self,
        task_id: int,
        agent_id: str | None = None,
        status: str = "pending",
        provider_id: int | None = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        session: Session | None = None,
        run_command: ConversationRunCommand | None = None,
    ) -> ConversationRunRecord:
        """Create a Conversation Run and initialize its canonical context.

        图片在创建阶段保存为 workspace-relative 路径；普通文件的展示文本与本机
        引用通过 ``ConversationRunExtra`` 保存，不新增附件表。

        参数:
            task_id: 所属任务标识。
            input_text: 本轮用户输入文本；传入 ``run_command`` 时由领域命令解析结果替代。
            status: 初始状态，默认 ``"pending"``。
            agent_id: 可选，本轮回绑定的 agent 标识；为 None 时回退到默认 ``"main_agent"``
                （与 Assistant Transport 主入口的默认值一致，非 ``"developer"``）。
            provider_id: 可选，模型归属厂商标识（指向 ``providers.id``）；None 表示未指定。
                与 ``model_name`` 配对出现：两者皆非 None 时按厂商能力校验模型；
                仅 ``model_name`` 非 None 而 ``provider_id`` 为 None 视为契约不完整，
                抛 ``ValueError``。
            model_name: 可选，本次请求使用的模型名；None 表示用户
                未选择模型（前端优先校验、后端兜底报错）。
            image_paths: 已按最终模型 capability 归一化后的 workspace-relative 图片路径。
            reasoning_effort: 可选，思考努力等级（low/high/max）；None 表示用户未指定。
            extra: 可选的 Run 扩展值对象；Assistant Transport 用其保存普通附件输入元数据。
            session: 可选，由上层跨表事务传入的数据库会话。传入时本方法不提交事务，
                由调用方统一提交；未传入时保持独立创建事务的行为。
            run_command: 可选的已归一化领域输入命令。传入时由本方法负责处理图片和普通
                文件附件，并覆盖 ``input_text``、``image_paths`` 与 ``extra``。

        返回:
            新创建的 ``ConversationRunRecord``；``input_text`` 保持正文，图片通过
            ``image_paths`` 结构化保存。

        异常:
            ValueError: 如果 ``input_text`` 为空或全空白，或模型不在厂商能力范围内。
            VisionNotSupportedError: 如果携带图片附件但模型不支持视觉输入。
            sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

        副作用:
            将命令行更新为一次持久化 Conversation Run（正文与附件结构化分离、
            ``image_paths`` 仅图片、``model_name`` 为解析后的最终模型名）；
            更新所属任务最新轮次信息；
            写入创建期图片构成日志。
        """
        # 厂商-模型契约校验：仅当两者皆非 None 时按厂商能力校验模型归属。
        # provider_id 为 None 但 model_name 已设，属契约不完整（前端应配对传入），
        # 显式抛 ValueError（由 API 层映射为 400），避免 get_provider(None) 误报 404。
        if model_name is not None and provider_id is None:
            raise ValueError(
                f"provider_id is required when model_name is set (model_name={model_name})"
            )
        input_text = None
        image_paths = None
        extra = None
        if provider_id is not None:
            provider = get_provider_service().get_provider(provider_id)
            provider_capability = ProviderCapability.get_capability(provider.name)
            if model_name not in provider_capability.models:
                raise ValueError(
                    f"model_name {model_name} not in provider capability "
                    f"{provider_capability.models}"
                )
        if run_command is not None:
            prepared = self._prepare_command(
                task_id,
                run_command,
                run_id=None,
                model_name=model_name,
            )
            input_text = prepared.input_text
            image_paths = prepared.image_paths
            extra = prepared.extra
        if input_text is None:
            raise ValueError("input_text is required when run_command is not provided")

        def persist_facts(persist_session: Session | None) -> ConversationRunRecord:
            run = self._run.create(
                task_id,
                input_text,
                status,
                agent_id=agent_id,
                provider_id=provider_id,
                model_name=model_name,
                image_paths=image_paths,
                reasoning_effort=reasoning_effort,
                extra=extra,
                session=persist_session,
            )
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
            service_depends.get_conversation_event_projector().process(
                RunInitializedEvent(task_id=task_id, run_id=run.id),
            )
        return run

    def reset_run_for_edit(
        self,
        run_id: int,
        provider_id: int | None = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        session: Session | None = None,
        run_command: ConversationRunCommand | None = None,
    ) -> ConversationRunRecord | None:
        """原地重置一个已结束 run，替换输入并创建新的 checkpoint 身份。

        仅允许非 active run 编辑；调用方负责在同一 task 锁内清理并重建 context。
        传入 ``run_command`` 时，本方法根据旧 Run 解析编辑请求中缺失的普通附件路径，
        并生成最终的文本、图片路径和 ``ConversationRunExtra``。传入 ``session`` 时复用
        外部事务且不自行提交。
        """
        input_text = None
        image_paths = None
        extra = None
        if run_command is not None:
            existing_run = self._run.get(run_id)
            prepared = self._prepare_command(
                existing_run.task_id,
                run_command,
                run_id=run_id,
                model_name=model_name,
            )
            input_text = prepared.input_text
            image_paths = prepared.image_paths
            extra = prepared.extra
        if input_text is None:
            raise ValueError("input_text is required when run_command is not provided")
        if not input_text.strip() and not image_paths:
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
            image_paths=image_paths,
            reasoning_effort=reasoning_effort,
            extra=extra,
            session=session,
        )

    def recover_orphaned_runs(
        self, end_reason: str = "runtime_restarted"
    ) -> list[ConversationRunRecord]:
        """把进程重启前遗留的 active Run 与其未闭合工具调用统一收口为 cancelled。

        崩溃或被强杀会留下两类未收敛事实，两者在**同一个事务**内收口：

        1. Run 行仍是 ``pending``/``running``，但驱动它的进程内执行器已随进程消失；
        2. 该 Run 最后一个 ``AIMessage`` 上的 ``tool_calls`` 没有结果行——工具可能已执行完但
           结果未落库，也可能根本没执行。

        收口方式：先以 ``pending/running -> cancelled`` 条件更新作为原子闸门（重复调用不会
        二次生效，也不会覆盖已被其他路径收敛的终态），再按该 Run 最后一个 ``AIMessage`` 补齐
        ``cancelled`` 占位 ``ToolMessage``（与运行时收口同一套配对规则与文案）。

        不处理 Transport snapshot：启动期没有订阅者，且冷读（懒加载）会按 Run 行与 context 行
        重建，Run 终态与工具 part 终态自然对齐，无需在恢复路径里额外投影。

        参数:
            end_reason: 写入 Run 的终态原因，默认 ``runtime_restarted``。

        返回:
            本次真正完成状态迁移的 Run 列表（按 ``created_at``、``id`` 升序）；无遗留 active
            Run、或迁移已被其他路径抢先时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 单个 Run 的收敛事务失败时向上传播；该 Run 保持原
            状态与上下文，下次启动重试。

        副作用:
            每个 Run 一个事务：更新 ``conversation_runs`` 终态并追加缺失的占位 ``ToolMessage``
            行。工具观察结果随 Run 一并收敛，其生命周期状态不另行复制。
        """

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
                    usage=None,
                    error=terminal_error(ConversationRunStatus.CANCELLED, end_reason),
                    session=session,
                )
                if record is not None:
                    repaired_tool_call_ids = self._context.close_unclosed_tool_calls_for_run(
                        run.task_id, run.id, session=session
                    )
            if record is None:
                continue
            recovered.append(record)
            log.info(
                "run_status_persisted",
                extra={
                    "msg": "orphan Run 终态与未闭合工具调用已收口为取消",
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

    def list_latest_runs(self) -> list[ConversationRunRecord]:
        """返回每个 task 最近一次 Run，供启动期 checkpoint 旁路恢复扫描使用。

        本方法只读取主库 Run 记录，不改变业务状态；调用方负责按需读取对应的 LangGraph
        checkpoint，并处理进程内 terminal worker。返回结果按最近创建时间倒序。
        """

        return self._run.list_latest_by_tasks()
