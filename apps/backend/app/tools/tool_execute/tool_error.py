"""失败工具观察的纯工厂。

本模块只承载一个纯函数：:func:`tool_error`。它是工具系统构造失败观察的
**唯一收口**：``ToolScheduler``（未知工具/权限拒绝/参数非法）、``ToolExecutor``
（启动失败/超时/handler 异常）以及各 handler（路径越界/无匹配等）的失败分支全部
经此构造，确保失败诊断字段（``error``/``reason``/``retryable``/``permission``）
在整个代码库的填充方式保持一致。
"""

import errno

from app.tools.schemas import ToolObservation


def os_error_message(exc: OSError, action: str) -> str:
    """把 ``OSError`` 转成面向模型友好的可读错误描述（共享助手）。

    该助手是文件类工具（read / write / patch / delete 等）构造失败观察时
    复用的唯一收口：把原始异常噪声（``[WinError 32] ...`` / ``[Errno 2] ...``）
    包成「动作 + 人读原因」的英文短句，便于模型一次理解并自行修正。

    参数:
        exc: 捕获到的操作系统异常。
        action: 正在进行的动作（如 ``"read the file"`` / ``"delete the target"``），
            用于构成主语，让模型立刻知道失败发生在哪一步。

    返回:
        英文、对模型友好的错误描述；优先用 ``strerror`` 人读原因，``strerror``
        缺失或为空时退化为异常原文，并清理可能存在的尾部标点以保持句式一致。

    异常:
        无。

    副作用:
        无（纯函数，只读 ``exc`` 属性）。
    """

    detail = (exc.strerror or str(exc)).strip().rstrip(". ")
    if not detail:
        code = exc.errno
        fallback = errno.errorcode.get(code) if code is not None else None
        detail = fallback or "unknown OS error"
    return f"could not {action}: {detail}"


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
        error: 「发生了什么错误」——面向模型的英文描述，点明失败动作与直接人读原因
            （如 ``could not write the file: permission denied``），**不得**塞原始
            异常噪声或堆栈摘要。该值同时写入 ``error`` 与 ``content`` 字段并直接回传
            给模型，模型据此立刻知道「错在哪一步、直接原因是什么」；开发者向的中文
            docstring/注释不在此限。
        reason: 「为什么失败、该如何修正、是否值得重试」——面向模型的**富文本**
            说明，**不是**稳定机器短码。须包含失败根因、可操作修正建议，以及与
            ``retryable`` 一致的重试提示（瞬态失败写「重试可能成功」，确定性失败写
            「须先修正再调用」）。该值回传给模型，供其理解失败并决定下一步动作；
            开发者向的中文 docstring/注释不在此限。
        retryable: 「原样重试是否可能成功」，默认 False。仅瞬态失败（如
            ``timeout``、临时文件占用）应传 True——用相同参数重试有意义；确定性
            失败（参数非法、路径越界等）保持 False，模型须先按 ``error`` 中的
            建议修正再调用。
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
