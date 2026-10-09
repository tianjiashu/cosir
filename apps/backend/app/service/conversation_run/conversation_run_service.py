"""Conversation Run 创建与编辑的用例编排。

单一职责：把领域命令编排成一次 Run 创建或原地编辑——解析输入与附件、在事务内落库 run 与
命令、更新所属 task 的最新 run，并按事务所有权决定是否发布 Transport 事件；另提供启动期的
遗留 active run 收口。

职责边界：
- 负责：``create_run`` 的用例编排、``recover_orphaned_runs`` 的
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
from app.models import (
    ConversationRunAttachmentInput,
    ConversationRunCommand,
    ConversationRunExtra,
    ConversationRunFileAttachment,
    ConversationRunRecord,
    ConversationRunStatus,
)
from app.service import depends as service_depends
from app.service.depends import get_model_config_service
from app.service.conversation_run.conversation_run_state_service import ConversationRunStateService
from app.service.conversation_run.conversation_task_context_service import ConversationTaskContextService
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
        workspace_id: int | None,
        command: ConversationRunCommand,
        *,
        run_id: int | None,
        supports_image: bool,
        reasoning_effort: str | None,
    ) -> _PreparedConversationRunInput:
        """把领域输入命令解析为 Run 持久化所需的最终事实。

        普通附件 token 只保留在 ``ConversationRunExtra.display_text``；传给模型的
        ``input_text`` 则把 token 替换为已经校验存在的本机文件或目录路径。图片顺序 marker 同样
        只存在于既有 ``display_text`` JSON 值中，并在模型输入边界移除。编辑命令可以从旧
        Run 恢复请求中省略的附件路径。图片附件在这里完成本次请求的数量、总大小和
        workspace 归属校验，并解析为 workspace-relative 路径；图片格式和尺寸规范化
        已经在上传边界完成。
        """

        file_attachments = self._resolve_file_attachments(
            command.attachments,
            run_id=run_id,
            display_text=command.display_text,
        )
        input_text = self._resolve_file_tokens(command.display_text, file_attachments)

        if command.image_asset_ids and not supports_image:
            raise ValueError("model does not support image")
        image_paths = self._resolve_image_assets(
            workspace_id,
            command.image_asset_ids,
        )
        if not input_text.strip() and not image_paths:
            raise ValueError("input_text must be a non-empty string")
        return _PreparedConversationRunInput(
            input_text=input_text,
            image_paths=image_paths,
            extra=ConversationRunExtra(
                display_text=command.display_text,
                attachments=file_attachments,
                reasoning_effort=reasoning_effort,
                ban_tools=command.ban_tools,
                propose_agent_configuration=command.propose_agent_configuration,
                propose_agent_team_configuration=command.propose_agent_team_configuration,
            ),
        )

    def _resolve_file_attachments(
        self,
        requested: list[ConversationRunAttachmentInput],
        *,
        run_id: int | None,
        display_text: str,
    ) -> list[ConversationRunFileAttachment]:
        """解析普通文件或目录附件 token，并从既有 Run 恢复编辑请求缺失的路径。"""

        existing_by_id: dict[str, ConversationRunFileAttachment] = {}
        if run_id is not None:
            try:
                existing_run = self._run.get(run_id)
                if existing_run.extra is not None:
                    existing_by_id = {
                        attachment["id"]: ConversationRunFileAttachment(
                            id=attachment["id"],
                            name=attachment["name"],
                            content_type=attachment["content_type"],
                            path=attachment["path"],
                        )
                        for attachment in existing_run.extra.attachments
                    }
            except KeyError:
                pass

        merged: dict[str, ConversationRunFileAttachment] = dict(existing_by_id)
        for attachment in requested:
            if not attachment.id or attachment.path is None:
                continue
            candidate = Path(attachment.path)
            if not (candidate.is_file() or candidate.is_dir()):
                raise ValueError("ordinary file attachment is unavailable")
            merged[attachment.id] = ConversationRunFileAttachment(
                id=attachment.id,
                name=attachment.name,
                content_type=attachment.content_type,
                path=attachment.path,
            )

        result: list[ConversationRunFileAttachment] = []
        for attachment_id in dict.fromkeys(Constant.Cosir.LOCAL_FILE_TOKEN.findall(display_text)):
            resolved_attachment = merged.get(attachment_id)
            if resolved_attachment is None or not (
                Path(resolved_attachment["path"]).is_file()
                or Path(resolved_attachment["path"]).is_dir()
            ):
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
    def _resolve_image_assets(
        workspace_id: int | None,
        image_asset_ids: list[str],
    ) -> list[str]:
        """校验本次图片聚合限制，并返回已上传图片的 workspace-relative 路径。"""

        if not image_asset_ids:
            return []
        if workspace_id is None:
            raise ValueError("workspace_id is required when image attachments are present")
        from app.service.attachment.attachment_service import AttachmentService

        return AttachmentService().resolve_image_assets_for_run(workspace_id, image_asset_ids)

    def create_run(
        self,
        task_id: int,
        agent_id: str | None = None,
        status: str = "pending",
        model_config_id: int | None = None,
        reasoning_effort: str | None = None,
        session: Session | None = None,
        run_command: ConversationRunCommand | None = None,
    ) -> ConversationRunRecord:
        """创建 Conversation Run 并初始化 canonical context。

        图片在创建阶段保存为 workspace-relative 路径；普通文件的展示文本与本机
        引用通过 ``ConversationRunExtra`` 保存，不新增附件表。

        参数:
            task_id: 所属任务标识。
            status: 初始状态，默认 ``"pending"``。
            agent_id: 可选，本轮回绑定的 agent 标识；为 None 时回退到默认 ``"main_agent"``
                （与 Assistant Transport 主入口的默认值一致，非 ``"developer"``）。
            model_config_id: 必填，模型连接配置标识；模型名称由该配置派生，不单独入参。
            reasoning_effort: 可选，统一推理强度（low/high/max）；None 表示用户未指定。
            session: 可选，由上层跨表事务传入的数据库会话。传入时本方法不提交事务，
                由调用方统一提交；未传入时保持独立创建事务的行为。
            run_command: 可选的已归一化领域输入命令。传入时由本方法解析普通附件，
                校验图片批次限制和模型图片能力，并生成最终 Run 输入事实。

        返回:
            新创建的 ``ConversationRunRecord``；``input_text`` 保持正文，图片通过
            ``image_paths`` 结构化保存。

        异常:
            ValueError: 如果输入为空、模型配置契约不完整，或携带图片但模型未配置图片能力。
            ImageNormalizationError: 图片附件不存在、越过 workspace 边界或违反统一批次限制。
            sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

        副作用:
            将命令行更新为一次持久化 Conversation Run（正文与附件结构化分离、
            ``image_paths`` 仅图片，模型名称由模型连接配置解析）；
            更新所属任务最新轮次信息；
            写入创建期图片构成日志。
        """
        if model_config_id is None:
            raise ValueError("model_config_id is required")
        config = get_model_config_service().get_config(model_config_id)
        if reasoning_effort is not None and not config.supports_reasoning_effort:
            raise ValueError("reasoning_effort is not supported by the selected model")
        supports_image = config.supports_image
        input_text = None
        image_paths = None
        extra = None
        if run_command is not None:
            workspace_id = (
                self._task.get(task_id).workspace_id
                if run_command.image_asset_ids
                else None
            )
            prepared = self._prepare_command(
                workspace_id,
                run_command,
                run_id=None,
                supports_image=supports_image,
                reasoning_effort=reasoning_effort,
            )
            input_text = prepared.input_text
            image_paths = prepared.image_paths
            extra = prepared.extra
        if input_text is None:
            raise ValueError("input_text is required when run_command is not provided")

        extra = ConversationRunExtra(
            display_text=extra.display_text if extra is not None else input_text,
            attachments=extra.attachments if extra is not None else [],
            ban_tools=extra.ban_tools if extra is not None else [],
            reasoning_effort=reasoning_effort,
            propose_agent_configuration=(
                extra.propose_agent_configuration if extra is not None else False
            ),
            propose_agent_team_configuration=(
                extra.propose_agent_team_configuration if extra is not None else False
            ),
        )
        context_window_total = config.context_window_k * 1000

        def persist_facts(persist_session: Session | None) -> ConversationRunRecord:
            run = self._run.create(
                task_id,
                input_text,
                status,
                agent_id=agent_id,
                model_config_id=model_config_id,
                image_paths=image_paths,
                extra=extra,
                session=persist_session,
            )
            self._task.set_current_run_id(
                task_id,
                run.id,
                context_window_total=context_window_total,
                session=persist_session,
            )
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
        """把进程重启前遗留的 active Run 与其未闭合工具调用统一收口为 cancelled。

        崩溃或被强杀会留下两类未收敛事实，两者在**同一个事务**内收口：

        1. Run 行仍是 ``pending``/``running``，但驱动它的进程内执行器已随进程消失；
        2. 该 Run 最后一个 ``AIMessage`` 上的 ``tool_calls`` 没有结果行——工具可能已执行完但
           结果未落库，也可能根本没执行。

        **等待用户决定的 Run（``waiting_for_input``）不在本方法收口**：它不是「驱动者消失」的
        遗留，而是图正停在 human-in-the-loop 断点上——图断点与决定通道都还在，用户回来仍能作答，
        收口它会让用户白等一场。它的终态只由取消 / 异常 / 用户决定收口（见
        ``ConversationRunStateService`` 的状态图）。

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
                        ConversationRunStatus.WAITING_FOR_INPUT.value,
                    ),
                    end_reason=end_reason,
                    usage=None,
                    error=ConversationRunStateService.terminal_error(
                        ConversationRunStatus.CANCELLED, end_reason
                    ),
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
