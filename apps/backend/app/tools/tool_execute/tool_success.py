"""成功工具观察的纯工厂。

本模块只承载一个纯函数：:func:`tool_success`。它是工具系统构造成功观察的
**唯一收口**，带 ``data`` 参数以承载结构化成功载荷，与 :func:`tool_error`
对称：``ToolExecutor._normalize_result`` 的兜底路径与各 handler 的成功分支
都应经此构造，确保成功观察的字段（``content``/``data``/``permission`` 等）
填充方式在整个代码库一致。
"""

from typing import Any

from app.tools.schemas import ToolDefinition, ToolObservation


def tool_success(
    tool: ToolDefinition,
    content: str,
    tool_call_id: str = "",
    data: dict[str, Any] | None = None,
) -> ToolObservation:
    """构造成功的工具观察结果（纯工厂函数）。

    参数:
        tool: 触发成功的工具定义，提供 ``tool_name`` 与 ``permission``
            （权限透传用于审计/展示）。
        content: 面向模型/用户的可读成功正文（即工具输出文本）。
            必须**用英文**撰写、对模型友好（简洁、结构化、便于模型直接消费与纠正）；
            开发者向的中文 docstring/注释不在此限。
        tool_call_id: 关联本次成功的模型工具调用 id；缺省为空字符串。
        data: 结构化结果载荷（自由键字典），承载不适合塞进 ``content`` 的
            机器可读字段（如 ``exit_code`` / ``type`` / ``recursive`` 等），
            供上层程序逻辑消费；与 ``content`` 互不替代、可同时填充；缺省为
            ``None``，构造时归一为空字典。

    返回:
        不可变的 :class:`ToolObservation`：``status="success"``，
        ``permission`` 透传自 ``tool``，``error``/``reason`` 为空，
        ``retryable=False``。

    异常:
        无。

    副作用:
        无（仅构造并返回新对象，不修改入参 ``tool``、不触发任何执行）。

    content 与 data 的区别:
        - ``content`` 是「人读文本」：给模型/用户看的故事（命令回显、文件
          摘要等），类型恒为 ``str``；必须英文、对模型友好（见 :func:`tool_success`
          的 ``content`` 参数约定）；失败时由 :func:`tool_error` 置为空。
        - ``data`` 是「机读字典」：给上层程序逻辑消费的账本（退出码、对象
          类型等），类型恒为 ``dict``；例如删除文件时 ``content`` 写「已删除
          文件 xxx」、``data`` 写 ``{"type": "file", "path": "..."}``，上层
          既能展示文本，也能不解析文本就直接拿到类型/路径做后续判断。
    """

    return ToolObservation(
        tool_name=tool.name,
        status="success",
        content=content,
        permission=tool.permission,
        tool_call_id=tool_call_id,
        data=data or {},
    )
