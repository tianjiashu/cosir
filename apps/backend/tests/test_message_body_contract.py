"""Session 消息类型的契约：类级元数据完整、kind/tag 唯一、注册表与联合一一对应。

ABC 只强制 ``render``；六个 ClassVar 漏写时要到被访问时才炸（``KIND`` 在注册表构造时、``WAKE``
在调度判定时）。这里用纯测试把契约锁在类型定义处，避免用 ``__init_subclass__`` 之类的运行期自查。
"""

from typing import ClassVar, get_args

import pytest

from app.core.workflow.message_queue.message_bodies import (
    _BODY_OF_KIND,
    MODEL_INPUT_BODIES,
    EnqueuedMessageBody,
    MessageBody,
    SessionMessageKind,
    ToolResultBody,
    WakeScope,
)
from app.core.workflow.message_queue.message_bodies.notice import (
    NOTICE_LEVELS,
    NOTICE_WAKE_SCOPES,
    NoticeKind,
)

_ENQUEUED = get_args(MessageBody)
"""入队消息类型；``ToolResultBody`` 不在其中（只写 context、不进队列）。"""

_REQUIRED = ("KIND", "TAG", "PROMPT_GUIDE", "PRIORITY", "DEFAULT_PROJECTED", "WAKE")


def test_every_enqueued_body_declares_the_full_contract() -> None:
    """每个入队 body 自己写下元数据：继承来的默认值会让新类型默默带上别的类型的语义。"""

    assert _ENQUEUED
    kinds = get_args(SessionMessageKind)
    for body in _ENQUEUED:
        assert issubclass(body, EnqueuedMessageBody)
        for name in _REQUIRED:
            assert name in vars(body), f"{body.__name__} 未声明 {name}"
        assert body.KIND in kinds
        assert isinstance(body.PRIORITY, int)
        assert isinstance(body.DEFAULT_PROJECTED, bool)
        assert body.WAKE in get_args(WakeScope)
        assert body.TAG
        assert body.PROMPT_GUIDE is None or (isinstance(body.PROMPT_GUIDE, str) and body.PROMPT_GUIDE)


def test_every_notice_kind_declares_level_and_wake_scope() -> None:
    """通知按种类细分级别与唤醒范围：漏登记的种类会在投递或调度时才炸。"""
    kinds = set(get_args(NoticeKind))
    assert set(NOTICE_LEVELS) == kinds == set(NOTICE_WAKE_SCOPES)
    # 只有「等着的东西没了」的告知能唤醒挂起的 Run，且都不开新 Run。
    assert {kind for kind, scope in NOTICE_WAKE_SCOPES.items() if scope != "none"} == {
        "derived_session_stopped",
        "backend_restarted",
    }
    assert "new_or_active" not in NOTICE_WAKE_SCOPES.values()


def test_kind_registry_round_trips_every_enqueued_body() -> None:
    """``kind`` 唯一，且持久化读回表与联合类型一一对应（漏登记会在读队列时才炸）。"""

    kinds = [body.KIND for body in _ENQUEUED]

    assert len(kinds) == len(set(kinds)), "kind 必须唯一"
    assert dict(zip(kinds, _ENQUEUED, strict=True)) == _BODY_OF_KIND


def test_model_input_bodies_are_tagged_and_unique() -> None:
    """进模型的类型都要有标签并声明读法（可为 None）；``tag`` 唯一，提示词才能按它列出消息协议。"""

    assert ToolResultBody in MODEL_INPUT_BODIES
    assert set(_ENQUEUED) <= set(MODEL_INPUT_BODIES)
    assert len(set(MODEL_INPUT_BODIES)) == len(MODEL_INPUT_BODIES)

    tags = [body.TAG for body in MODEL_INPUT_BODIES]
    assert len(set(tags)) == len(tags), "tag 必须唯一"
    for body in MODEL_INPUT_BODIES:
        assert "PROMPT_GUIDE" in vars(body), f"{body.__name__} 未声明 PROMPT_GUIDE"
        assert callable(body.render)


def test_enqueued_base_rejects_a_body_without_render() -> None:
    """基类真的抽象：漏写 ``render`` 的类型无法被实例化，不会产出无法写进 context 的消息。"""

    class WithoutRender(EnqueuedMessageBody):
        KIND: ClassVar[SessionMessageKind] = "user_input"
        TAG: ClassVar[str] = "probe"
        PROMPT_GUIDE: ClassVar[str | None] = "probe"
        PRIORITY: ClassVar[int] = 1
        DEFAULT_PROJECTED: ClassVar[bool] = False
        WAKE: ClassVar[WakeScope] = "none"

    with pytest.raises(TypeError):
        WithoutRender()
