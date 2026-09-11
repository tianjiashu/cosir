"""Conversation event 契约回归。

本文件只校验事件契约本身（判别式路由、字段约束、不可变性与联合穷尽性），不校验任何
投影行为——投影由后续 applier 承担。

穷尽性断言是本文件的核心价值：新增事件类型时若忘记登记进 ``ConversationEvent`` 判别式
联合，本测试立即失败，避免「事件已定义但消费者永远收不到」的静默缺陷。
"""

from typing import Literal, get_args

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.assistant_transport.event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    ContextUsageUpdatedEvent,
    ConversationEvent,
    ConversationEventEnvelope,
    RunInitializedEvent,
    RunStatusChangedEvent,
    ToolCallCreatedEvent,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
    UserInputAppendedEvent,
)
from app.models.enums.conversation_run_status import ConversationRunStatus

_ADAPTER: TypeAdapter[ConversationEvent] = TypeAdapter(ConversationEvent)

# 每种事件的最小合法载荷与其期望类型；``type`` 是判别式路由的唯一依据。
_EVENT_PAYLOADS: list[tuple[dict[str, object], type[BaseModel]]] = [
    ({"type": "run_initialized", "task_id": 1, "run_id": 1}, RunInitializedEvent),
    (
        {"type": "user_input_appended", "task_id": 1, "run_id": 1, "text": "读一下这个文件"},
        UserInputAppendedEvent,
    ),
    (
        {
            "type": "run_status_changed",
            "task_id": 1,
            "run_id": 1,
            "status": "completed",
            "end_reason": None,
        },
        RunStatusChangedEvent,
    ),
    (
        {
            "type": "assistant_text_delta",
            "task_id": 1,
            "run_id": 1,
            "step_id": "step-2",
            "part": "reasoning",
            "delta": "先看目录结构",
        },
        AssistantTextDeltaEvent,
    ),
    (
        {"type": "assistant_part_closed", "task_id": 1, "run_id": 1, "part": "text"},
        AssistantPartClosedEvent,
    ),
    (
        {
            "type": "tool_call_created",
            "task_id": 1,
            "run_id": 1,
            "tool_call_id": "1:step-2:0",
            "tool_name": "read_file",
        },
        ToolCallCreatedEvent,
    ),
    (
        {
            "type": "tool_call_status_changed",
            "task_id": 1,
            "run_id": 1,
            "tool_call_id": "1:step-2:0",
            "status": "failed",
            "args": {"path": "app/app.py"},
            "error": "path outside workspace",
        },
        ToolCallStatusChangedEvent,
    ),
    (
        {
            "type": "tool_calls_settled",
            "task_id": 1,
            "run_id": 1,
            "status": "cancelled",
            "reason": "user_cancelled",
        },
        ToolCallsSettledEvent,
    ),
    (
        {"type": "context_usage_updated", "task_id": 1, "run_id": 1, "ratio": 0.42},
        ContextUsageUpdatedEvent,
    ),
]


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    _EVENT_PAYLOADS,
    ids=[str(payload["type"]) for payload, _ in _EVENT_PAYLOADS],
)
def test_discriminator_routes_to_expected_type(
    payload: dict[str, object], expected_type: type[BaseModel]
) -> None:
    """``type`` 判别式必须把每种载荷精确路由到对应事件类型。

    参数:
        payload: 最小合法事件载荷。
        expected_type: 期望反序列化得到的事件类。

    返回:
        无。

    异常:
        AssertionError: 路由到错误类型时抛出（由 pytest 报告）。

    副作用:
        无；只做内存内校验。
    """

    event = _ADAPTER.validate_python(payload)
    assert type(event) is expected_type


def test_discriminator_rejects_unknown_type() -> None:
    """未知事件类型必须被判别式拒绝，而不是退化成基类或静默丢弃。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 未抛出 ``ValidationError`` 时抛出（由 pytest 报告）。

    副作用:
        无；只做内存内校验。
    """

    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"type": "not_a_known_event", "task_id": 1, "run_id": 1})


def test_envelope_defaults_and_optional_fields() -> None:
    """信封的可选字段必须有稳定缺省，且缺省不参与快照投影语义。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 缺省值与预期不一致时抛出（由 pytest 报告）。

    副作用:
        无；只做内存内校验。
    """

    event = RunInitializedEvent(task_id=7, run_id=42)
    assert event.step_id is None
    assert event.occurred_at.tzinfo is not None
    assert event.model_dump()["task_id"] == 7


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "user_input_appended", "task_id": 1, "run_id": 1, "text": ""},
        {"type": "assistant_text_delta", "task_id": 1, "run_id": 1, "part": "text", "delta": ""},
        {
            "type": "assistant_text_delta",
            "task_id": 1,
            "run_id": 1,
            "part": "unknown",
            "delta": "x",
        },
        {
            "type": "tool_call_created",
            "task_id": 1,
            "run_id": 1,
            "tool_call_id": "",
            "tool_name": "x",
        },
        {
            "type": "tool_calls_settled",
            "task_id": 1,
            "run_id": 1,
            "status": "completed",
            "reason": "r",
        },
        {"type": "context_usage_updated", "task_id": 1, "run_id": 1, "ratio": -0.1},
        {"type": "run_status_changed", "task_id": 1, "run_id": 1, "status": "half_done"},
        {"type": "run_initialized", "task_id": 0, "run_id": 1},
        {"type": "run_initialized", "task_id": 1, "run_id": 1, "typo_field": 1},
        {"type": "context_usage_updated", "task_id": 1, "run_id": 1, "ratio": True},
        {"type": "context_usage_updated", "task_id": 1, "run_id": 1, "used_tokens": True},
    ],
    ids=[
        "empty_user_text",
        "empty_delta",
        "unknown_part",
        "empty_tool_call_id",
        "settled_to_non_terminal",
        "negative_ratio",
        "unknown_run_status",
        "non_positive_task_id",
        "unknown_field",
        "boolean_ratio",
        "boolean_context_tokens",
    ],
)
def test_constraints_reject_invalid_payloads(payload: dict[str, object]) -> None:
    """字段约束必须拦住空值、越界、非法枚举与拼错字段。

    参数:
        payload: 非法事件载荷。

    返回:
        无。

    异常:
        AssertionError: 非法载荷未被拒绝时抛出（由 pytest 报告）。

    副作用:
        无；只做内存内校验。
    """

    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(payload)


def test_event_is_frozen() -> None:
    """事实一旦产生必须不可变，避免消费者改写后污染后续消费者。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 允许就地改写字段时抛出（由 pytest 报告）。

    副作用:
        无；只做内存内校验。
    """

    event = UserInputAppendedEvent(task_id=1, run_id=1, text="原始输入")
    with pytest.raises(ValidationError):
        event.text = "被改写"


def test_run_status_uses_domain_enumeration() -> None:
    """run 状态必须复用领域枚举，不允许在事件层出现第二套状态词表。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 状态未解析为 ``ConversationRunStatus``、或 JSON 序列化结果
            不是稳定落库字符串时抛出（由 pytest 报告）。

    副作用:
        无；只做内存内校验。
    """

    event = RunStatusChangedEvent(
        task_id=1, run_id=1, status=ConversationRunStatus.CANCELLED, end_reason="user_cancelled"
    )
    assert event.status is ConversationRunStatus.CANCELLED
    assert event.model_dump(mode="json")["status"] == "cancelled"
    # 稳定落库字符串必须能直接回灌为枚举，保证日志回放与测试无需构造枚举实例。
    parsed = RunStatusChangedEvent.model_validate(
        {"task_id": 1, "run_id": 1, "status": "cancelled"}
    )
    assert parsed.status is ConversationRunStatus.CANCELLED


def test_union_covers_every_event_type() -> None:
    """每个已定义的事件类型都必须登记进判别式联合。

    这是新增事件时的回归防线：只加类而忘记加联合，会让该事件永远无法被反序列化
    消费，且不会有任何静态报错。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 存在未登记或已删除的事件类型时抛出（由 pytest 报告）。

    副作用:
        无；只做类型反射。
    """

    declared = set(ConversationEventEnvelope.__subclasses__())
    registered = set(get_args(get_args(ConversationEvent)[0]))
    assert declared == registered


def test_event_type_literals_are_unique() -> None:
    """各事件的 ``type`` 字面量必须互不相同，否则判别式路由会产生歧义。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 出现重复的 ``type`` 字面量时抛出（由 pytest 报告）。

    副作用:
        无；只做类型反射。
    """

    literals: list[str] = []
    for member in get_args(get_args(ConversationEvent)[0]):
        args = get_args(member.model_fields["type"].annotation)
        assert args, f"{member.__name__} 的 type 字段缺少 Literal 标注"
        literals.extend(args)
    assert len(literals) == len(set(literals))


def test_type_annotation_shape_is_literal() -> None:
    """事件的 ``type`` 字段必须是 ``Literal``，供静态判别与测试反射使用。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: ``type`` 字段不是 ``Literal`` 标注时抛出（由 pytest 报告）。

    副作用:
        无；只做类型反射。
    """

    event: ConversationEvent = RunInitializedEvent(task_id=1, run_id=1)
    annotation = type(event).model_fields["type"].annotation
    assert get_args(annotation) == ("run_initialized",)
    assert isinstance(event.type, str)
    assert isinstance(get_args(Literal["run_initialized"])[0], str)
