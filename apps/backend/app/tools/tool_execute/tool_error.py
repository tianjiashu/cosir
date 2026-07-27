"""失败工具观察的纯工厂。

本模块只承载一个纯函数：:func:`tool_error`。它是工具系统构造失败观察的
**唯一收口**：``ToolScheduler``（未知工具/权限拒绝/参数非法）、``ToolExecutor``
（启动失败/超时/handler 异常）以及各 handler（路径越界/无匹配等）的失败分支全部
经此构造，确保失败诊断字段（``error``/``reason``/``retryable``/``permission``）
在整个代码库的填充方式保持一致。
"""

from app.tools.schemas import ToolObservation


def tool_error(
    tool_name: str,
    error: str,
    reason: str,
    retryable: bool = False,
    permission: str = "",
    tool_call_id: str = "",
) -> ToolObservation:
    """构造失败的工具观察结果（纯工厂函数）。

    参数:
        tool_name: 触发失败的工具名称。
        error: 失败的错误描述（异常消息、堆栈摘要或人读说明）。必须**用英文**撰写、
            对模型友好——该值同时写入 ``error`` 与 ``content`` 字段并直接回传给模型，
            模型据此判断失败原因并修正下一步动作；开发者向的中文 docstring/注释不在此限。
        reason: 失败分类短码，供上层区分失败性质并决定重试策略；常见取值包括
            ``unknown_tool`` / ``permission_denied`` / ``invalid_arguments`` /
            ``handler_exception`` / ``timeout`` / ``handler_start_failed`` /
            ``path_escape`` 等。
        retryable: 是否可安全重试，默认 False；仅瞬态失败（如 ``timeout``）应传
            True，供上层重放决策。
        permission: 触发工具所需权限标识（用于审计/展示），默认空字符串；权限被
            拒时由调用方回填被拒的权限值。
        tool_call_id: 关联的模型工具调用 id，默认空字符串。

    返回:
        不可变的 :class:`ToolObservation`：``status="error"``，``content`` 与
        ``error`` 均包含人类可读错误，其余诊断字段按入参填充，``data`` 为空字典。

    异常:
        无。

    副作用:
        无（仅构造并返回新对象，不修改任何入参、不触发任何执行）。
    """

    return ToolObservation(
        tool_name=tool_name,
        status="error",
        content=error,
        error=error,
        reason=reason,
        retryable=retryable,
        permission=permission,
        tool_call_id=tool_call_id,
    )
