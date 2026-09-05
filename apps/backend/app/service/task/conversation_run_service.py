"""Conversation Run orchestration service.

单一职责：编排 run 的创建与管理——创建 run 时同步更新所属任务的最新 run ID 和消息预览。

职责边界：
- 负责：run 创建（含任务最新 run 更新）、run 查询与状态更新、pending run 的原子启动。
- 不负责：直接 SQL 操作（委托给 ``ConversationRunCrud``/``TaskCrud``）；不负责对话消息事实读写
  （由 Transport snapshot owner 与 Task context owner 负责）。
"""

from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.llm_provider.capability.model_capability import ModelCapability
from app.core.llm_provider.capability.provider_capability import ProviderCapability
from app.core.workflows.event import (
    RunInitializedEvent,
    RunStatusChangedEvent,
    UserInputAppendedEvent,
)
from app.models import ConversationRunRecord, ConversationRunStatus
from app.models.attachment_ref import AttachmentRef
from app.models.errors.llm_provider_exceptions import VisionNotSupportedError
from app.service import depends as service_depends
from app.service.depends import get_provider_service
from app.storage.store_engines import main_session_factory
from app.utils.file_utils import render_attachment_refs_to_text

# 图片后缀事实源统一收口于 app.utils.constants.IMAGE_EXTENSIONS；本服务不再直接引用，
# 视觉粗判改由 attachments 的 kind 字段判定。真实格式/体积校验在运行期
# build_user_content_blocks 完成。


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
        self._session_factory = main_session_factory()

    def create_run(
        self,
        task_id: int,
        input_text: str,
        agent_id: str | None = None,
        status: str = "pending",
        provider_id: int | None = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        attachments: list[AttachmentRef] | None = None,
        session: Session | None = None,
    ) -> ConversationRunRecord:
        """Create a Conversation Run and initialize its parent task snapshot.

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
            attachments: 可选，本轮携带的结构化附件列表；None 表示无附件。
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

        attachments = attachments or []
        image_paths = [att.ref for att in attachments if att.kind == "image"] or None

        # 视觉能力拦截（构建期业务规则）：若本轮携带图片且模型不支持视觉输入，
        # 提前报错（422 语义，由 API 层映射为 HTTP 422 + 中文引导），避免运行期才失败。
        if image_paths:
            if model_name is None:
                raise ValueError("model_name is required when image attachments are present")
            model_capability = ModelCapability.get_capability(model_name)
            if not model_capability.supports_image:
                log.warning(
                    "create_run vision rejected",
                    extra={
                        "task_id": task_id,
                        "provider_id": provider_id,
                        "model_name": model_name,
                        "image_count": len(image_paths),
                        "reason": "model_not_support_image",
                    },
                )
                raise VisionNotSupportedError(f"model {model_name} does not support image input")

        # 非图片附件渲染为文本前缀并拼进 input_text；空渲染结果不拼接。
        attachment_text = render_attachment_refs_to_text(
            [att for att in attachments if att.kind != "image"]
        )
        if attachment_text:
            input_text = f"{attachment_text}\n\n{input_text}"

        log.info(
            "create_run attachments",
            extra={
                "task_id": task_id,
                "provider_id": provider_id,
                "model_name": model_name,
                "image_count": len(image_paths or []),
                "non_image_count": len(attachments) - len(image_paths or []),
            },
        )

        if session is not None:
            run = self._run.create(
                task_id,
                input_text,
                status,
                agent_id=agent_id,
                provider_id=provider_id,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                image_paths=image_paths,
                session=session,
            )
            return run

        with self._session_factory.begin() as managed_session:
            run = self._run.create(
                task_id,
                input_text,
                status,
                agent_id=agent_id,
                provider_id=provider_id,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                image_paths=image_paths,
                session=managed_session,
            )
        projector = service_depends.get_conversation_event_projector()
        projector.process(RunInitializedEvent(task_id=task_id, run_id=run.id))
        projector.process(UserInputAppendedEvent(task_id=task_id, run_id=run.id, text=input_text))
        return run

    def get_run(self, run_id: int) -> ConversationRunRecord:
        return self._run.get(run_id)

    def list_runs_for_task(self, task_id: int) -> list[ConversationRunRecord]:
        return self._run.list_by_task(task_id)

    def list_recoverable(self) -> list[ConversationRunRecord]:
        """返回应用启动时可恢复的 pending/running 运行。"""
        return self._run.list_recoverable()

    def complete_run_if_running(self, run_id: int) -> ConversationRunRecord | None:
        """Complete a running Conversation Run atomically.

        业务语义：仅 ``running`` 可进入 ``completed`` 终态并落库回复文本；约束收敛在
        本方法（service 层），CRUD 层只做通用的「状态白名单 + 原子更新」。

        参数:
            run_id: 待完成的 Conversation Run 标识。

        返回:
            成功完成时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 run 状态为 completed。
        """

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.COMPLETED.value,
            (ConversationRunStatus.RUNNING.value,),
            None,
        )
        if record is None:
            return None
        service_depends.get_conversation_event_projector().process(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.COMPLETED,
            )
        )
        return self._run.get(run_id)

    def complete_run_with_message(
        self, run_id: int, message: AIMessage, end_reason: str | None = None
    ) -> ConversationRunRecord | None:
        """将 Run 标记 completed，并更新其 snapshot 展示状态。"""

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.COMPLETED.value,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            end_reason,
        )
        if record is None:
            return None
        service_depends.get_conversation_event_projector().process(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.COMPLETED,
                end_reason=end_reason,
            )
        )
        return self._run.get(run_id)

    def fail_run_if_running(
        self, run_id: int, end_reason: str | None = None
    ) -> ConversationRunRecord | None:
        """Fail a running Conversation Run atomically.

        业务语义：仅 ``running`` 可进入 ``failed`` 终态；约束收敛在本方法（service 层），
        CRUD 层只做通用的「状态白名单 + 原子更新」。

        参数:
            run_id: 待失败落定的 Conversation Run 标识。
            end_reason: 可选失败原因。

        返回:
            成功失败落定时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 run 状态为 failed（并可选写入 end_reason）。
        """

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.FAILED.value,
            (ConversationRunStatus.RUNNING.value,),
            end_reason,
        )
        if record is None:
            return None
        service_depends.get_conversation_event_projector().process(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.FAILED,
                end_reason=end_reason,
            )
        )
        return self._run.get(run_id)

    def fail_run_if_pending_or_running(
        self, run_id: int, end_reason: str | None = None
    ) -> ConversationRunRecord | None:
        """将尚未启动或正在执行的 Conversation Run 原子落定为 failed。"""
        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.FAILED.value,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            end_reason,
        )
        if record is None:
            return None
        service_depends.get_conversation_event_projector().process(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.FAILED,
                end_reason=end_reason,
            )
        )
        return self._run.get(run_id)

    def cancel_run_if_running(
        self, run_id: int, end_reason: str = "user_cancelled"
    ) -> ConversationRunRecord | None:
        """将 active Run 标记 cancelled，并更新 snapshot 展示状态。"""

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.CANCELLED.value,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            end_reason,
        )
        if record is None:
            return None
        service_depends.get_conversation_event_projector().process(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.CANCELLED,
                end_reason=end_reason,
            )
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
            条件满足时更新 run 状态为 running。
        """
        row = self._run.update_status_if_in(
            run_id=run_id,
            target_status=ConversationRunStatus.RUNNING.value,
            allowed_statuses=(ConversationRunStatus.PENDING.value,),
        )
        return row is not None
