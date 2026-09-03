"""失败工具观察的纯工厂与共享文本助手。

本模块承载失败观察的**唯一收口** :func:`tool_error`，以及工具层复用的若干
共享文本助手（:func:`os_error_message`、:func:`blocked_device_reason`、
:func:`handler_exception_reason`）。
``ToolScheduler``（未知工具/权限拒绝/参数非法）、``ToolExecutor``（启动失败/超时/
handler 异常）以及各 handler（路径越界/无匹配等）的失败分支全部经此构造，确保失败
诊断字段（``error``/``reason``/``retryable``/``permission``）在整个代码库的填充方式
保持一致。

取消类观察的文案与工厂收口在 :mod:`app.tools.tool_execute.tool_cancelled`，不在本模块——
取消是「主动中止」而非「执行故障」，其 reason 语义（是否重试由用户指令决定）与失败
观察不同，单独归口以避免与确定性失败的重试提示混淆。
"""

import dataclasses
import errno

from app.models.enums.error_kind import ErrorKind
from app.core.tools.schemas import ToolObservation


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


def blocked_device_reason(action: str) -> str:
    """构造「路径指向 OS 设备/敏感伪文件」的富文本 ``reason``（共享助手）。

    文件类工具（read / write / search 等）命中 ``ProjectPathResolver`` 的 blocked
    设备分支时共用这一模板，避免各 handler 重复长串；按 ``action`` 动名词定制提示，
    与确定性失败的「same path will always be rejected」重试提示保持一致。

    参数:
        action: 受影响的动作英文动名词（如 ``"read"`` / ``"written"`` /
            ``"searched recursively"``），用于定制说明。

    返回:
        面向模型的富文本说明（根因 + 修正建议 + 确定性失败的重试提示）。

    异常:
        无。

    副作用:
        无（纯函数）。
    """

    return (
        f"the requested path points to an OS device or sensitive pseudo-file "
        f"(e.g. NUL/CON/COM1 on Windows, /dev/* or /proc/* on POSIX) and cannot be "
        f"{action}; pass a regular file path inside the project instead. The same "
        f"path will always be rejected, so choose a different target."
    )


def internal_execution_error_reason(header: str) -> str:
    """构造「执行链内部错误（非工具语义失败）」的富文本 ``reason`` 共享尾部。

    该助手是执行器在调用 ``ToolScheduler.execute`` 时捕获到**非工具语义异常**（即
    执行链自身 bug：调度器内部、事件构造、trace span、序列化等抛出的意外异常，而非
    工具 handler 主动返回的业务失败）的唯一收口：区分于工具语义失败，明确告诉模型
    「工具本体没跑起来，是 runtime 出了内部错误」，并给出确定性失败的重试提示。

    参数:
        header: 已点明失败位置与人读原因的英文短句（如 ``"internal execution error
            before the tool ran: ..."``），作为说明前缀。

    返回:
        面向模型的富文本说明（根因已由 ``header`` 给出 + 修正建议 + 确定性失败
        的重试提示：需先排查 runtime 内部错误，原样重试无效）。

    异常:
        无。

    副作用:
        无（纯函数）。
    """
    return (
        f"{header} the tool itself never ran, so this is an internal runtime failure "
        f"rather than a tool-reported error; retrying with identical arguments will "
        f"fail again until the runtime issue is fixed."
    )


def handler_exception_reason(header: str) -> str:
    """构造「handler 抛异常 / 进程崩溃」类失败富文本 ``reason`` 的共享尾部。

    该助手是 :class:`ToolExecutor` 各 ``handler_exception`` 失败分支（进程通信
    断裂、子进程 handler 抛异常、线程内 handler 抛异常）复用的唯一收口：统一追加
    「确定性失败 + 原样重试无效 + 先读 message 修正根因」的提示，确保三处语义一致，
    且与 ``retryable=False`` 的重试信号保持一致。

    参数:
        header: 已点明失败动作与人读原因的英文短句（如 ``"the tool handler raised
            an exception: ..."``），作为说明前缀。

    返回:
        面向模型的富文本说明（根因已由 ``header`` 给出 + 修正建议 + 确定性失败
        的重试提示）。

    异常:
        无。

    副作用:
        无（纯函数）。
    """

    return (
        f"{header} this is a deterministic failure from the tool, so retrying with "
        f"identical arguments will fail again; read the message to fix the underlying "
        f"cause before calling the tool again."
    )


def tool_error(
    tool_name: str,
    error: str,
    reason: str,
    retryable: bool = False,
    permission: str = "",
    tool_call_id: str = "",
    display_data: dict[str, object] | None = None,
    error_kind: ErrorKind = ErrorKind.RUNTIME_FAILED,
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
        display_data: 仅供客户端展示消费的结构化数据；会 merge 进
            ``ToolObservation.display_data``（不含 ``content`` 副本），**不会回传给
            模型**。为 error 观察携带结构化诊断（如语法检查的 ``syntax_errors``）而
            增补，与 :func:`tool_success` 的 ``display_data`` 语义对称。
        error_kind: 供日志、运行时事件与回放分析使用的稳定错误分类，例如
            ``parse_invalid``、``unknown_tool``、``schema_invalid``、``runtime_failed``、
            ``permission_denied``。

    返回:
        不可变的 :class:`ToolObservation`：``status="error"``，``content`` 与
        ``error`` 均包含人类可读错误，其余诊断字段按入参填充。

    异常:
        无。

    副作用:
        无（仅构造并返回新对象，不修改任何入参、不触发任何执行）。
    """
    observation = ToolObservation(
        tool_name=tool_name,
        status="error",
        content=error,
        error=error,
        reason=reason,
        retryable=retryable,
        permission=permission,
        tool_call_id=tool_call_id,
    )
    # 与 tool_success 对称：display_data 不承载 content 副本，避免大体积错误文本
    # 经 display_data 旁路无约束进入前端事件流与可观测性平台。
    merged = dataclasses.asdict(observation)
    merged.pop("content", None)
    merged.pop("display_data", None)
    if display_data:
        merged.update(display_data)
    merged["error_kind"] = error_kind.value
    observation.data = merged
    return observation
