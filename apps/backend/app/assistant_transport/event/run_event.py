"""Conversation Run 生命周期事件。

本模块只承载「一次 Conversation Run 自身的创建与状态迁移」这一单一职责：run 骨架的建立、
run 执行状态的迁移（含终态）。run 内消息与工具的细节事实不在此模块。每个事件把自身的投影
逻辑实现在 ``plan`` 中。

状态词表直接复用 ``ConversationRunStatus``（领域枚举单一事实源），不在本模块重复字面量。

不负责：assistant 消息内容与工具调用生命周期（分别见 ``message_event``、
``tool_call_event``）；本模块只定义用户输入事实事件的 Transport parts。
"""
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, model_validator

from app.assistant_transport.event.conversation_event_envelope import (
    ConversationEventEnvelope,
)
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import (
    ConversationStateFilePart,
    ConversationStateImagePart,
    ConversationStateTextPart,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import ConversationRunError
from app.models.enums.conversation_run_status import ConversationRunStatus

# Run 状态迁移白名单：投影层只放行领域侧已确认的迁移，其余（陈旧、重复投递）一律丢弃。
# ``cancelled -> running`` 是「续跑恢复」的合法迁移，由
# ``ConversationRunStateService.resume_cancelled_run`` 在数据库条件更新
# （``WHERE status='cancelled'``）成功之后才发出；若在此丢弃，snapshot 会永久
# 停在终态，后续 tool-call 创建事件随之被终态短路丢弃，最终在工具状态迁移事件上抛 KeyError 中断
# 整个 Run。``completed`` / ``failed`` 仍不可逆，避免迟到事件把已结束的 Run 拉回 active。
_ALLOWED_RUN_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"pending", "running", "completed", "failed", "cancelled"}),
    "running": frozenset({"running", "completed", "failed", "cancelled"}),
    "cancelled": frozenset({"cancelled", "running"}),
    "failed": frozenset({"failed"}),
    "completed": frozenset({"completed"}),
}


class RunInitializedEvent(ConversationEventEnvelope):
    """一个 Conversation Run 已被创建并绑定到 Task。

    事实语义：命令已被幂等占用、run 已落库，Transport 侧应为其建立 user / assistant
    两条消息骨架与 ``run.runId`` 基线。本事件**不携带用户输入文本**——文本由紧随其后的
    ``UserInputAppendedEvent`` 追加，使「run 已存在但输入尚未写完」这个中间态也是合法且
    可渲染的。编辑重跑也可用 ``replace_existing`` 重建骨架。

    Attributes:
        replace_existing: 编辑重跑时是否替换已有 Run 的 Transport 骨架。

    异常:
        pydantic.ValidationError: 信封字段非法或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    # 判别式字段显式给出默认值：生产者不必重复书写字面量，判别式路由行为不变。
    type: Literal["run_initialized"] = "run_initialized"
    replace_existing: bool = False

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划 Run 的 user/assistant 消息骨架。

        参数:
            state: 当前 Task snapshot。

        返回:
            建立 user / assistant 消息骨架与 ``run`` 基线的 mutation 列表；普通初始化在
            已存在同一 Run 时回空列表（幂等），``replace_existing`` 则用于编辑重跑。

        异常:
            无。

        副作用:
            无业务副作用；丢弃骨架建立的快照分叉分支会写 WARNING 日志（观测旁路）。
        """

        existing_index = next(
            (index for index, run in enumerate(state["runs"]) if run["runId"] == self.run_id),
            None,
        )
        if existing_index is not None:
            if not self.replace_existing:
                return []
            return [
                ConversationStateMutation(
                    "set",
                    ("runs", existing_index),
                    self._run_snapshot(),
                ),
                ConversationStateMutation("set", ("current_run_id",), self.run_id),
                ConversationStateMutation("set", ("context_usage_ratio",), None),
                ConversationStateMutation("set", ("context_usage_used",), None),
                ConversationStateMutation("set", ("context_window_total",), None),
                ConversationStateMutation("set", ("error",), None),
            ]

        current_run_id = state["current_run_id"]
        if self.run_id is None:
            return []
        if current_run_id is not None and (
            self.run_id <= current_run_id
            or not state["runs"]
            or state["runs"][-1]["status"] not in Constant.Run.TERMINAL_STATUSES
        ):
            # 走到这里说明 snapshot 与 DB 已分叉（新 run 被当作陈旧事件丢弃）：不阻断
            # 主流程，但必须留痕，否则排障时只能看到僵尸现象看不到丢弃本身。
            log.warning(
                "transport_run_initialized_skeleton_dropped",
                extra={
                    "msg": "RunInitializedEvent 未建立骨架：快照存在更新的未结束 run，疑似分叉",
                    "data": {
                        "task_id": self.task_id,
                        "run_id": self.run_id,
                        "current_run_id": current_run_id,
                        "latest_run_status": state["runs"][-1]["status"] if state["runs"] else None,
                    },
                },
            )
            return []
        if any(run["runId"] == self.run_id for run in state["runs"]):
            return []
        offset = len(state["runs"])
        return [
            ConversationStateMutation(
                "set",
                ("runs", offset),
                {
                    "runId": self.run_id,
                    "status": "pending",
                    "endReason": None,
                    "messages": self._messages(),
                    "usage": None,
                    "error": None,
                },
            ),
            ConversationStateMutation("set", ("current_run_id",), self.run_id),
            ConversationStateMutation("set", ("context_usage_ratio",), None),
            ConversationStateMutation("set", ("context_usage_used",), None),
            ConversationStateMutation("set", ("context_window_total",), None),
            ConversationStateMutation("set", ("error",), None),
        ]

    def _messages(self) -> list[ConversationStateMessage]:
        """Build the empty user/assistant skeleton without user input content."""

        return [
            self._message(f"user-{self.run_id}", "user", []),
            self._message(f"assistant-{self.run_id}", "assistant", []),
        ]

    def _run_snapshot(self) -> dict[str, object]:
        """Build the reset Run snapshot used by the edit projection."""

        return {
            "runId": self.run_id,
            "status": "pending",
            "endReason": None,
            "messages": self._messages(),
            "usage": None,
            "error": None,
        }


class RunStatusChangedEvent(ConversationEventEnvelope):
    """一次 Conversation Run 的执行状态已经迁移。

    事实语义：**迁移已经在领域侧完成并落库成功后**才发出本事件（event 是事实而非意图）。
    发出方不负责再迁一次，applier 只把它投影为快照的 ``run.status`` 与对应 assistant
    消息的 ``status`` / ``endReason``。

    所有迁移（pending → running → completed / failed / cancelled，以及启动恢复、
    API 取消、执行器收口）统一由本类型表达，不为每个目标状态单开事件类型：
    类型数与 applier 分支数减半，且「哪些迁移合法」的判定集中在领域侧一处。

    Attributes:
        status: 迁移后的 run 状态，取值受 ``ConversationRunStatus`` 约束。
        end_reason: 终态原因（如 ``"invalid_model_output"`` / ``"user_cancelled"`` /
            ``"backend_restarted"``）；非终态迁移为 ``None``。
        usage_stats: Run 终态时随事件附带的完整累计 token 用量；未获得 provider usage 时为
            ``None``。
        error: Run 终态的受控错误契约（稳定 ``code`` + provider 无关的用户文案）；非终态
            迁移与 ``completed`` 为 ``None``，此时投影会把已存在的错误清空。

    异常:
        pydantic.ValidationError: ``status`` 不在枚举内，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["run_status_changed"] = "run_status_changed"
    status: ConversationRunStatus
    end_reason: str | None = None
    usage_stats: ConversationRunUsageStats | None = None
    error: ConversationRunError | None = None

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划 Run 与 assistant 消息的状态迁移。

        参数:
            state: 当前 Task snapshot。

        返回:
            更新 ``run`` 状态、终态原因与受控错误（及可选 ``usage``）的 mutation；若 assistant
            message 存在，额外更新其 ``status`` / ``endReason``，并把仍 running 的
            text / reasoning part 收口为 completed。目标状态不在白名单矩阵内时（陈旧或重复
            投递的迁移）返回空列表。

        异常:
            KeyError: 事件引用的 Run 或 assistant message 不存在。

        副作用:
            无。
        """

        run_index = self._find_run(state, self.run_id)
        current_status = str(state["runs"][run_index]["status"])
        if self.status.value not in _ALLOWED_RUN_STATUS_TRANSITIONS.get(
            current_status, frozenset()
        ):
            # 白名单丢弃对重复投递是常态，但若因投影失败丢掉了唯一一次合法迁移，
            # snapshot 会永久停在旧状态；留痕区分两种情况。
            log.warning(
                "transport_run_status_transition_dropped",
                extra={
                    "msg": "RunStatusChangedEvent 不在迁移白名单内，投影被丢弃",
                    "data": {
                        "task_id": self.task_id,
                        "run_id": self.run_id,
                        "snapshot_status": current_status,
                        "incoming_status": self.status.value,
                    },
                },
            )
            return []
        mutations: list[ConversationStateMutation] = [
            ConversationStateMutation("set", ("runs", run_index, "status"), self.status.value),
            ConversationStateMutation("set", ("runs", run_index, "endReason"), self.end_reason),
            ConversationStateMutation("set", ("runs", run_index, "error"), self.error),
        ]
        if self.usage_stats is not None:
            incoming_usage = self.usage_stats.to_dict()
            current_usage = state["runs"][run_index]["usage"]
            # 用量只在终态事件中写入；保留单调替换保护，避免重复投递或恢复流程中
            # 较小的终态摘要覆盖已经确认的累计值。
            if current_usage is None or all(
                current_usage[key] is None
                or incoming_usage[key] is None
                or current_usage[key] <= incoming_usage[key]
                for key in current_usage
            ):
                mutations.append(
                    ConversationStateMutation("set", ("runs", run_index, "usage"), incoming_usage)
                )
        message_index = self._find_assistant_message(state, self.run_id, required=False)
        if message_index is None:
            return mutations
        _, assistant_index = message_index
        base = ("runs", run_index, "messages", assistant_index)
        if self.status.value in Constant.Run.TERMINAL_STATUSES:
            for part_index, part in enumerate(
                state["runs"][run_index]["messages"][assistant_index]["parts"]
            ):
                if (
                    isinstance(part, dict)
                    and part.get("type") in {"text", "reasoning"}
                    and part.get("status") == "running"
                ):
                    mutations.append(
                        ConversationStateMutation(
                            "set", (*base, "parts", part_index, "status"), "completed"
                        )
                    )
        return mutations


UserInputPart = ConversationStateTextPart | ConversationStateImagePart | ConversationStateFilePart


def build_user_input_parts(
    display_text: str,
    image_paths: Sequence[str],
    file_attachments: Sequence[Mapping[str, str]],
) -> list[UserInputPart]:
    """Build ordered safe Transport parts from persisted user-input facts.

    The existing ``display_text`` value carries internal image/file markers solely to preserve
    the composer order across the existing Run JSON extra field. The markers are converted to
    safe locator parts here; the resulting parts contain no local path or binary content. Old
    records without image markers retain the historical image-path append fallback.

    Raises:
        ValueError: If a display token has no matching attachment or no part can be built.
    """

    attachments_by_id: dict[str, Mapping[str, str]] = {}
    for attachment_record in file_attachments:
        attachment_id = attachment_record.get("id")
        if (
            not attachment_id
            or not Constant.Transport.FILE_LOCATOR.fullmatch(f"cosir-local-file:{attachment_id}")
            or not attachment_record.get("name")
            or not attachment_record.get("content_type")
        ):
            raise ValueError("ordinary file attachment is malformed")
        attachments_by_id[attachment_id] = attachment_record

    image_paths_by_id: dict[str, str] = {}
    for path in image_paths:
        image_id = Path(path).name.split(".", 1)[0]
        locator = f"cosir-attachment://{image_id}"
        if not Constant.Transport.IMAGE_LOCATOR.fullmatch(locator):
            raise ValueError("image attachment locator is invalid")
        image_paths_by_id[image_id] = path

    parts: list[UserInputPart] = []
    cursor = 0
    referenced_image_ids: set[str] = set()
    referenced_file_ids: set[str] = set()
    for match in Constant.Transport.INPUT_TOKEN.finditer(display_text):
        text_part = display_text[cursor:match.start()]
        if text_part:
            parts.append({"type": "text", "text": text_part, "status": "completed"})
        marker_kind, token_id = match.groups()
        if marker_kind == "file":
            attachment = attachments_by_id.get(token_id)
            if attachment is None:
                raise ValueError("ordinary file attachment is unavailable")
            if token_id in referenced_file_ids:
                cursor = match.end()
                continue
            parts.append(
                {
                    "type": "file",
                    "file": f"cosir-local-file:{attachment['id']}",
                    "name": attachment["name"],
                    "contentType": attachment["content_type"],
                }
            )
            referenced_file_ids.add(token_id)
        else:
            if token_id not in image_paths_by_id:
                raise ValueError("image attachment is unavailable")
            if token_id in referenced_image_ids:
                cursor = match.end()
                continue
            parts.append({"type": "image", "image": f"cosir-attachment://{token_id}"})
            referenced_image_ids.add(token_id)
        cursor = match.end()
    remaining_text = display_text[cursor:]
    if remaining_text:
        parts.append({"type": "text", "text": remaining_text, "status": "completed"})
    parts.extend(
        {"type": "image", "image": f"cosir-attachment://{image_id}"}
        for image_id in image_paths_by_id
        if image_id not in referenced_image_ids
    )
    if not parts:
        raise ValueError("user input event must contain at least one part")
    return parts


class UserInputAppendedEvent(ConversationEventEnvelope):
    """用户本轮完整输入已落库并追加到该 Run 的 user 消息。

    事实语义：canonical context 已成功写入本轮 HumanMessage，Transport 侧应将完整且有序
    的文本、图片和普通文件 parts 一次性写入 user 消息。附件 part 只携带受控 locator 与
    展示元数据；本事件不读取磁盘、不查询附件内容，也不生成附件文件。

    异常:
        pydantic.ValidationError: parts 为空、包含 reasoning/tool part、locator 非法或
            普通文件 part 缺少展示字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["user_input_appended"] = "user_input_appended"
    parts: list[UserInputPart] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_parts(self) -> "UserInputAppendedEvent":
        """Validate the user-only Transport part subset and its safe locators."""

        for part in self.parts:
            part_type = part.get("type")
            if part_type == "text":
                text_part = cast(ConversationStateTextPart, part)
                if set(text_part) - {"type", "text", "status"} or not text_part["text"]:
                    raise ValueError("user text part is malformed")
                if text_part.get("status") not in {None, "completed"}:
                    raise ValueError("user text part status is invalid")
            elif part_type == "image":
                image_part = cast(ConversationStateImagePart, part)
                if (
                    set(image_part) != {"type", "image"}
                    or not Constant.Transport.IMAGE_LOCATOR.fullmatch(image_part["image"])
                ):
                    raise ValueError("user image part locator is invalid")
            elif part_type == "file":
                file_part = cast(ConversationStateFilePart, part)
                if (
                    set(file_part) != {"type", "file", "name", "contentType"}
                    or not Constant.Transport.FILE_LOCATOR.fullmatch(file_part["file"])
                    or not file_part["name"]
                    or not file_part["contentType"]
                ):
                    raise ValueError("user file part is malformed")
            else:
                raise ValueError("user input event contains an unsupported part")
        return self

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划 user 消息完整有序 parts 的一次性写入。

        参数:
            state: 当前 Task snapshot。

        返回:
            单条 ``set`` mutation（将完整有序 parts 写入 user 消息）。

        异常:
            KeyError: 该 run 的 user text part 不存在。

        副作用:
            无。
        """

        run_index = self._find_run(state, self.run_id)
        message_index = next(
            (
                index
                for index, message in enumerate(state["runs"][run_index]["messages"])
                if message["role"] == "user"
            ),
            None,
        )
        if message_index is None:
            raise KeyError(f"user message for run {self.run_id} not found")
        current_parts = state["runs"][run_index]["messages"][message_index]["parts"]
        next_parts: list[dict[str, object]] = [
            cast(dict[str, object], dict(part)) for part in self.parts
        ]
        if current_parts == next_parts:
            return []
        return [
            ConversationStateMutation(
                "set",
                ("runs", run_index, "messages", message_index, "parts"),
                next_parts,
            )
        ]
