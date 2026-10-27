"""失败工具观察的纯工厂与共享文本助手。

本模块承载失败观察的**唯一收口** :func:`tool_error`，以及文件类工具复用的
:func:`os_error_message`。``ToolAccessGate``（未知工具/权限拒绝/参数非法）、
``ToolHandlerRunner``（启动失败/超时/handler 异常）以及各 handler（路径越界/无匹配等）的
失败分支全部经此构造。交给模型的失败文本（错误事实 + 下一步建议）由调用点直接写成一段
``content``：只有调用点知道该失败能否靠改参数修好，重试与否用自然语言写进建议里，
工厂不再拼装固定的 ``error:`` / ``hint:`` / ``reason:`` 模板。

取消类观察的文案与工厂收口在 :mod:`app.core.tool.results.tool_cancelled`，不在本模块——
取消是「主动中止」而非「执行故障」，其 reason 语义（是否重试由用户指令决定）与失败
观察不同，单独归口以避免与确定性失败的重试提示混淆。
"""

import errno

from app.core.tool.schemas import ToolObservation
from app.core.tool.schemas.tool_names import (
    TOOL_APPLY_PATCH,
    TOOL_DELEGATE_TASK,
    TOOL_DELETE_FILE,
    TOOL_EXECUTE_TERMINAL,
    TOOL_FIND_FILES,
    TOOL_LIST_DIRECTORY,
    TOOL_MOVE_FILE,
    TOOL_READ_FILE,
    TOOL_REPLACE,
    TOOL_SEARCH_CONTENT,
    TOOL_WEB_EXTRACT,
    TOOL_WEB_SEARCH,
    TOOL_WRITE_FILE,
)

_DEFAULT_STATUS_HINTS = {
    TOOL_READ_FILE: "读取失败",
    TOOL_WRITE_FILE: "写入失败",
    TOOL_REPLACE: "替换失败",
    TOOL_APPLY_PATCH: "补丁失败",
    TOOL_DELETE_FILE: "删除失败",
    TOOL_MOVE_FILE: "移动失败",
    TOOL_SEARCH_CONTENT: "搜索失败",
    TOOL_FIND_FILES: "搜索失败",
    TOOL_LIST_DIRECTORY: "列举失败",
    TOOL_EXECUTE_TERMINAL: "命令失败",
    TOOL_WEB_SEARCH: "搜索失败",
    TOOL_WEB_EXTRACT: "提取失败",
    TOOL_DELEGATE_TASK: "委派失败",
}
STATUS_HINT_MAX_LENGTH = 8


def normalize_status_hint(tool_name: str, status_hint: object) -> str:
    """Return the bounded UI hint used by failed tool observations.

    The persisted transport result is untrusted reconstruction input, so its hint
    follows the same fallback rule as :func:`tool_error`: only a non-empty,
    trimmed string within the UI length budget is retained.  Otherwise the
    tool's controlled default (or the generic failure hint) is returned.
    """

    hint = status_hint.strip() if isinstance(status_hint, str) else ""
    if not hint or len(hint) > STATUS_HINT_MAX_LENGTH or not hint.isprintable():
        return _DEFAULT_STATUS_HINTS.get(tool_name, "执行失败")
    return hint


def os_error_message(exc: OSError, action: str) -> str:
    """把 ``OSError`` 转成面向模型友好的可读错误描述（共享助手）。

    该助手是文件类工具（read_file / replace / apply_patch 等）构造失败观察时
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
    content: str,
    tool_call_id: str = "",
    status_hint: str | None = None,
) -> ToolObservation:
    """构造失败的工具观察结果（纯工厂函数）。

    参数:
        tool_name: 触发失败的工具名称。
        content: 原样交给模型的英文失败文本：先点明失败动作与直接人读原因（如
            ``could not write the file: permission denied``），再给下一步建议；**不得**塞原始
            异常噪声或堆栈摘要。
        tool_call_id: 关联的模型工具调用 id，默认空字符串。
        status_hint: 客户端短提示，建议约 5 个字，最长 8 个字符；不会回传给模型。省略时按
            ``tool_name`` 选择工具默认提示。错误观察的 ``display_data`` 固定只包含该提示，
            不承载目标、结果或完整错误原因。
    """
    observation = ToolObservation(
        tool_name=tool_name,
        status="error",
        content=content,
        tool_call_id=tool_call_id,
    )
    # 错误 UI 通道只保留后端显式传入的短提示；完整失败文本仅供模型通道。
    observation.display_data = {"status_hint": normalize_status_hint(tool_name, status_hint)}
    return observation
