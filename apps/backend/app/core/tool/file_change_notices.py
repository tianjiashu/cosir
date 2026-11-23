"""core 对「读过的文件有没有变化要告诉模型」的端口：依赖倒置，core 不导入 ``app.tools``。

文件系统归工具层（``app/tools/file_system``），core 只存放与透传文件事实、不解读；每次模型请求前
``notices.request_notices`` 经这里问一次。实现由 lifespan 装配时注入（测试由 conftest 注入），未注入就
调用属于装配缺陷，直接报错，不静默降级。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import JsonValue

from app.core.workflow.message_queue.message_bodies.notice import NoticeBody
from app.models.message_context.message_metadata.tool_message_metadata import ToolResultFacts


class FileChangeNotices(Protocol):
    def notice(
        self,
        session_id: int,
        visible: Sequence[ToolResultFacts],
        baseline: list[dict[str, JsonValue]],
    ) -> NoticeBody | None:
        """``visible`` 是本次请求模型可见的结果事实（上下文顺序），``baseline`` 是上一条同类通知记下的
        基准（原样）；有新变化时返回通知。"""
        ...


_notices: FileChangeNotices | None = None


def set_file_change_notices(notices: FileChangeNotices | None) -> None:
    """装配期注入实现；``None`` 解除。"""
    global _notices
    _notices = notices


def file_change_notices() -> FileChangeNotices:
    """取已注入的实现。

    异常:
        RuntimeError: 尚未装配。
    """
    if _notices is None:
        raise RuntimeError("file change notices are not assembled")
    return _notices
