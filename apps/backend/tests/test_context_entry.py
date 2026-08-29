"""ContextEntry 值对象测试。"""

from app.core.context.context_entry import ContextEntry
from app.models import RuntimeMessage


def test_context_entry_preserves_turn_ownership() -> None:
    """ContextEntry 应独立保存消息与 turn 归属，不承载是否进模型的标记。"""
    entry = ContextEntry(
        message=RuntimeMessage(role="user", content_text="x"),
        turn_id=11,
    )

    assert entry.message.content_text == "x"
    assert entry.turn_id == 11
    assert not hasattr(entry, "include_in_context")
