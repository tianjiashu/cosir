"""Conversation Run 生命周期事件。

本模块只承载「一次 Conversation Run 自身的创建与状态迁移」这一单一职责：run 骨架的建立、
run 执行状态的迁移（含终态）。run 内消息与工具的细节事实不在此模块。每个事件把自身的投影
逻辑实现在 ``plan`` 中。

状态词表直接复用 ``ConversationRunStatus``（领域枚举单一事实源），不在本模块重复字面量。

不负责：用户输入的文本内容（见 ``message_event``）、工具调用生命周期（见 ``tool_call_event``）。
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from app.assistant_transport.event.conversation_event_envelope import (
    ConversationEventEnvelope,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.models.enums.conversation_run_status import ConversationRunStatus

# Run 状态迁移白名单：投影层只放行领域侧已确认的迁移，其余（陈旧、重复投递）一律丢弃。
# ``cancelled -> running`` 是「续跑恢复」的合法迁移，由
# ``ConversationRunService.resume_cancelled_run`` 在数据库条件更新
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
    两条空消息骨架与 ``run.runId`` 基线。本事件**不携带用户输入文本**——文本由紧随其后的
    ``UserInputAppendedEvent`` 追加，与流式 assistant 文本共用同一条 ``append-text`` 通道，
    使「run 已存在但输入尚未写完」这个中间态也是合法且可渲染的。

    Attributes:
        仅继承信封字段；本事件无额外 payload。

    异常:
        pydantic.ValidationError: 信封字段非法或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    # 判别式字段显式给出默认值：生产者不必重复书写字面量，判别式路由行为不变。
    type: Literal["run_initialized"] = "run_initialized"

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划 Run 的 user/assistant 消息骨架。

        参数:
            state: 当前 Task snapshot。

        返回:
            建立 user / assistant 两条空消息骨架与 ``run`` 基线的 mutation 列表；若该 run
            的消息骨架已存在则回空列表（幂等）。

        异常:
            无。

        副作用:
            无。
        """

        current_run_id = state["current_run_id"]
        if self.run_id is None:
            return []
        if current_run_id is not None and (
            self.run_id <= current_run_id
            or not state["runs"]
            or state["runs"][-1]["status"] not in {"completed", "failed", "cancelled"}
        ):
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
                    "messages": [
                        self._message(
                            f"user-{self.run_id}",
                            "user",
                            [{"type": "text", "text": "", "status": "completed"}],
                        ),
                        self._message(f"assistant-{self.run_id}", "assistant", []),
                    ],
                    "usage": None,
                },
            ),
            ConversationStateMutation("set", ("current_run_id",), self.run_id),
            ConversationStateMutation("set", ("context_usage_ratio",), None),
            ConversationStateMutation("set", ("context_usage_used",), None),
            ConversationStateMutation("set", ("context_window_total",), None),
            ConversationStateMutation("set", ("error",), None),
        ]


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

    异常:
        pydantic.ValidationError: ``status`` 不在枚举内，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["run_status_changed"] = "run_status_changed"
    status: ConversationRunStatus
    end_reason: str | None = None
    usage_stats: ConversationRunUsageStats | None = None

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划 Run 与 assistant 消息的状态迁移。

        参数:
            state: 当前 Task snapshot。

        返回:
            更新 ``run`` 状态（及可选 ``usage``）的 mutation；若 assistant message 存在，
            额外更新其 ``status`` / ``endReason``，并把仍 running 的 text / reasoning part
            收口为 completed。目标状态不在白名单矩阵内时（陈旧或重复投递的迁移）返回空列表。

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
            return []
        mutations: list[ConversationStateMutation] = [
            ConversationStateMutation("set", ("runs", run_index, "status"), self.status.value),
            ConversationStateMutation("set", ("runs", run_index, "endReason"), self.end_reason),
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
        if self.status.value in {"completed", "failed", "cancelled"}:
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


class UserInputAppendedEvent(ConversationEventEnvelope):
    """用户输入文本已追加到该 Run 的 user 消息。

    事实语义：用户输入已落库为 canonical user 消息，Transport 侧应把这段文本增量追加到
    该 run 的 user 消息 text part。之所以叫「追加」而不是「设置」：与
    ``AssistantTextDeltaEvent`` 同构，复用同一条 ``append-text`` 投影通道，避免为
    「一次性写入」和「流式写入」维护两套投影逻辑。

    Attributes:
        text: 非空用户输入文本增量。

    异常:
        pydantic.ValidationError: ``text`` 为空串，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["user_input_appended"] = "user_input_appended"
    text: str = Field(min_length=1)

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划 user text part 的增量追加。

        参数:
            state: 当前 Task snapshot。

        返回:
            单条 ``append-text`` mutation（向 user 消息的 text part 追加输入文本）。

        异常:
            KeyError: 该 run 的 user text part 不存在。

        副作用:
            无。
        """

        run_index, message_index, part_index = self._find_message_part(
            state, self.run_id, "user", "text"
        )
        return [
            ConversationStateMutation(
                "append-text",
                ("runs", run_index, "messages", message_index, "parts", part_index, "text"),
                self.text,
            )
        ]
