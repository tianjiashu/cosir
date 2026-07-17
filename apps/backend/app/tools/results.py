"""工具内部结果到模型观测结果的转换。"""

from dataclasses import dataclass
from typing import Optional

from app.tools.schemas import ToolObservation


@dataclass(frozen=True)
class ToolRuntimeResult:
    """表示 Tool Runtime 单次调用的归一化结果。

    参数:
        observation: 返回给工作流和模型的观测结果。
        artifact_id: 可选已落盘产物标识。

    返回:
        不可变运行时结果。

    异常:
        无。

    副作用:
        无。
    """

    observation: ToolObservation
    artifact_id: Optional[str] = None


class ToolObservationBuilder:
    """构建低 token 的工具观测结果。"""

    def __init__(self, max_content_chars: int = 4000) -> None:
        """初始化观测结果摘要限制。

        参数:
            max_content_chars: 允许直接返回给模型的最大字符数。

        返回:
            无。

        异常:
            ValueError: 当最大长度小于 1 时抛出。

        副作用:
            保存内容限制。
        """

        if max_content_chars < 1:
            raise ValueError("max_content_chars must be greater than zero")
        self._max_content_chars = max_content_chars

    def success(
        self,
        tool_name: str,
        content: str,
        permission: str,
        tool_call_id: str = "",
        artifact_id: Optional[str] = None,
    ) -> ToolObservation:
        """构造成功工具观测并裁剪过长的直接输出。

        参数:
            tool_name: 工具名称。
            content: handler 的文本输出或 artifact 摘要。
            permission: 工具权限。
            tool_call_id: 可选持久化工具调用标识。
            artifact_id: 可选已落盘 artifact 标识。

        返回:
            归一化成功观测。

        异常:
            无。

        副作用:
            无。
        """

        summarized = content[: self._max_content_chars]
        if len(content) > self._max_content_chars:
            summarized = f"{summarized}\n[output truncated; inspect artifact for full content]"
        return ToolObservation(
            tool_name=tool_name,
            status="success",
            content=summarized,
            permission=permission,
            approval_status="allow",
            tool_call_id=tool_call_id,
            artifact_id=artifact_id,
        )

    def error(
        self,
        tool_name: str,
        error: str,
        permission: str = "",
        approval_status: str = "",
        tool_call_id: str = "",
    ) -> ToolObservation:
        """构造不执行额外副作用的失败观测。

        参数:
            tool_name: 工具名称。
            error: 面向模型的错误摘要。
            permission: 工具权限。
            approval_status: 策略或审批状态。
            tool_call_id: 可选持久化工具调用标识。

        返回:
            归一化失败观测。

        异常:
            无。

        副作用:
            无。
        """

        return ToolObservation(
            tool_name=tool_name,
            status="error",
            content="",
            error=error,
            permission=permission,
            approval_status=approval_status,
            tool_call_id=tool_call_id,
        )
