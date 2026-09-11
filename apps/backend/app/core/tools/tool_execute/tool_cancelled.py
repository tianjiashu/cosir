
from app.core.tools.schemas import ToolObservation

# 执行前被取消（call 边界检测到取消信号、尚未执行）时复用的 reason 模板。
# 措辞要点：取消是「用户/系统主动中止」，不是「执行故障」，因此**不替用户决定
# 是否重试**——是否重试取决于用户后续指令或当前上下文，模型不应把取消默认当成
# 不可重试的终态。``retryable=False`` 仅表示「本次未执行、无失败结果可重试」，而非
# 「用户禁止重试」。
CANCEL_NOT_EXECUTED_REASON = (
    "the tool call was cancelled before it started executing and produced no result; "
    "this is an active stop initiated by the user or system, not a tool failure, so "
    "whether to retry is decided by the user's next instruction or the surrounding "
    "context rather than being assumed non-retryable. adapt your plan based on the "
    "cancellation and any new instruction."
)


def tool_cancelled(
    tool_name: str,
    reason: str,
    error: str = "",
    permission: str = "",
    tool_call_id: str = "",
) -> ToolObservation:
    """构造取消态的工具观察结果（纯工厂函数）。

    与 :func:`tool_error` 对称，专用于「用户主动中断导致工具未正常完成」的取消态，
    例如父 turn 被取消导致 delegate_task 子 Agent 中止、或执行器在 call 边界检测到取消
    信号后为未执行的调用补占位。取消与失败语义不同：根因是主动中止而非执行故障，模型
    不应将其当作「需修正参数后重试」的失败。``retryable`` 固定为 ``False`` 仅表示本次
    未执行、无可重试的失败结果，并不代表用户禁止重试——是否重试应由用户后续指令或上下文
    决定；``error`` 字段携带「发生了什么」的英文描述供模型直接理解。

    参数:
        tool_name: 被取消的工具名称。
        reason: 「为什么被取消」——面向模型的富文本说明；执行前取消可复用模块级常量
            ``CANCEL_NOT_EXECUTED_REASON``（明确「主动中止、是否重试由用户指令决定」，
            不默认不可重试）。
        error: 「发生了什么的取消描述」——面向模型的英文短句（如 ``the delegated child
            agent was cancelled because the parent turn was cancelled``）；缺省时复用
            ``reason`` 首句，保证 ``error`` 字段不为空、模型可读。
        permission: 触发工具所需权限标识（用于审计/展示），默认空字符串。
        tool_call_id: 关联的模型工具调用 id，默认空字符串。

    返回:
        不可变的 :class:`ToolObservation`：``status="cancelled"``，``retryable=False``
        （语义为「本次未执行、无失败结果」而非「禁止重试」），``content`` 与 ``error``
        均包含取消描述。

    异常:
        无。

    副作用:
        无（仅构造并返回新对象，不修改任何入参、不触发任何执行）。
    """
    if not error:
        error = reason.split(".", 1)[0] if reason else "the tool call was cancelled"
    observation = ToolObservation(
        tool_name=tool_name,
        status="cancelled",
        content=error,
        error=error,
        reason=reason,
        retryable=False,
        permission=permission,
        tool_call_id=tool_call_id,
    )
    return observation
