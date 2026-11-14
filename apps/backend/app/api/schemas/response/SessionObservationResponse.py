"""会话观测页的响应：Session 的全部消息行（不论是否进了模型上下文），以及相关 Run 的运行节点。"""

from datetime import datetime
from typing import Any, cast

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam
from pydantic import BaseModel

from app.core.llm_provider.message_codec import display_arguments
from app.core.llm_provider.openai_model import usage_from_response
from app.models.message_metadata import (
    AssistantMessageMetadata,
    ToolMessageMetadata,
    UserMessageMetadata,
)
from app.models.session_context import SessionContextRecord
from app.service.conversation.session_observation import (
    ObservedRun,
    ObservedStatusChange,
    SessionObservation,
)


class ObservedMessageResponse(BaseModel):
    """一条按消息角色投影的上下文持久化记录。"""

    id: int | None
    session_id: int
    turn: int
    # 继承来的历史（分叉复制，没有本地 Run）。
    inherited: bool
    sequence: int
    include_in_context: bool
    # 是否投影进对话界面；与 ``include_in_context`` 独立（内部通知进模型但不显示）。
    project_to_transport: bool
    # 被撤回的本地行：既不进模型也不显示，只留给观测（``SessionContextRecord.retained`` 的反面）。
    withdrawn: bool
    created_at: str
    updated_at: str
    role: str
    # user 行的来源种类（``MessageKind``）；所有队列消息进模型时都是 user 角色，区分靠它。
    kind: str | None = None
    content: Any
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    usage_metadata: dict[str, Any] | None = None
    tool_call_id: str | None = None
    # tool 行与回答某次调用的 user 行所对应的工具名；发起调用的行不在这个 Session 里时为空。
    tool_name: str | None = None
    status: str | None = None

    @classmethod
    def from_record(
        cls, record: SessionContextRecord, tool_names: dict[str, str]
    ) -> "ObservedMessageResponse":
        """将持久化记录投影为前端使用的角色字段。"""

        message = record.message
        values: dict[str, Any] = {
            "id": record.id,
            "session_id": record.session_id,
            "turn": record.turn,
            "inherited": record.run_id is None and record.sequence > 0,
            "sequence": record.sequence,
            "include_in_context": record.include_in_context,
            "project_to_transport": record.project_to_transport,
            "withdrawn": not record.retained,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "content": message.get("content"),
            "role": message["role"],
        }
        if message["role"] == "assistant":
            values["reasoning_content"] = message.get("reasoning_content")
            values["tool_calls"] = [
                {
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "args": display_arguments(call["function"]["arguments"]),
                }
                for call in cast(
                    list[ChatCompletionMessageFunctionToolCallParam],
                    message.get("tool_calls") or [],
                )
            ]
            if isinstance(record.metadata, AssistantMessageMetadata):
                values["usage_metadata"] = usage_from_response(record.metadata.usage)
        elif message["role"] == "tool" and isinstance(record.metadata, ToolMessageMetadata):
            call_id = message["tool_call_id"]
            values.update(
                tool_call_id=call_id,
                tool_name=tool_names.get(call_id),
                status=record.metadata.status,
            )
        elif isinstance(record.metadata, UserMessageMetadata):
            answered = record.metadata.tool_call_id
            values.update(
                kind=record.metadata.kind,
                tool_call_id=answered,
                tool_name=tool_names.get(answered) if answered is not None else None,
            )
        return cls(**values)


class ObservedStatusChangeResponse(BaseModel):
    """一个运行节点：Run 在 ``at`` 从 ``previous_status`` 到达 ``status``；创建那一条没有来源。"""

    at: str
    status: str
    previous_status: str | None
    previous_at: str | None
    end_reason: str | None
    # 这个 Run 目前最后一次迁移：Run 的结局与用量挂在它上面展示。
    latest: bool

    @classmethod
    def from_change(cls, change: ObservedStatusChange) -> "ObservedStatusChangeResponse":
        return cls(
            at=change.at,
            status=change.status,
            previous_status=change.previous_status,
            previous_at=change.previous_at,
            end_reason=change.end_reason,
            latest=change.latest,
        )


class ObservedRunResponse(BaseModel):
    """一个 Run 的状态事实与运行节点。结局与用量属于整个 Run，不拆到回合。

    不带 Run 号：诊断按回合进入，排查日志时用 Session id 与这里的起止时间收窄范围。按回合查询时
    ``changes`` 只含落在这一回合时间窗里的节点，其余字段仍是整个 Run 的。
    """

    status: str
    end_reason: str | None
    error_code: str | None
    usage: dict[str, Any] | None
    started_at: datetime
    updated_at: datetime
    changes: list[ObservedStatusChangeResponse]

    @classmethod
    def from_observed(cls, observed: ObservedRun) -> "ObservedRunResponse":
        run = observed.run
        return cls(
            status=run.status,
            end_reason=run.end_reason,
            error_code=run.error["code"] if run.error is not None else None,
            usage=dict(run.usage) if run.usage is not None else None,
            started_at=run.created_at,
            updated_at=run.updated_at,
            changes=[ObservedStatusChangeResponse.from_change(c) for c in observed.changes],
        )


class SessionObservationResponse(BaseModel):
    """观测页一次查询的结果；按回合查询时 ``runs`` 至多一个，继承来的回合没有。"""

    session_title: str
    # Session 里最大的回合号，不随所选回合变化。
    last_turn: int
    messages: list[ObservedMessageResponse]
    runs: list[ObservedRunResponse]

    @classmethod
    def from_observation(cls, observation: SessionObservation) -> "SessionObservationResponse":
        return cls(
            session_title=observation.title,
            last_turn=observation.last_turn,
            messages=[
                ObservedMessageResponse.from_record(row, observation.tool_names)
                for row in observation.rows
            ],
            runs=[ObservedRunResponse.from_observed(run) for run in observation.runs],
        )
