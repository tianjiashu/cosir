"""成功工具观察的纯工厂。

本模块只承载一个纯函数：:func:`tool_success`。它是工具系统构造成功观察的
**唯一收口**，带 ``data`` 参数以承载结构化成功载荷，与 :func:`tool_error`
对称：``ToolExecutor._normalize_result`` 的兜底路径与各 handler 的成功分支
都应经此构造，确保成功观察的字段（``content``/``data``/``permission`` 等）
填充方式在整个代码库一致。
"""

import dataclasses

from app.core.tools.schemas import ToolObservation


def tool_success(
    tool_name: str,
    permission: str,
    content: str,
    tool_call_id: str = "",
    data: dict[str, object] | None = None,
) -> ToolObservation:
    """构造成功的工具观察结果（纯工厂函数）。

    参数:
        tool_name: 触发本次成功的工具名称（与 :class:`ToolDefinition.name` 对应）。
        permission: 触发工具所需的权限标识（透传自 :class:`ToolDefinition`），
            便于上层做审计/展示；失败因权限被拒时仍会回填被拒的权限值。
        content: 面向模型/用户的可读成功正文（即工具输出文本）。
            必须**用英文**撰写、对模型友好（简洁、结构化、便于模型直接消费与纠正）；
            开发者向的中文 docstring/注释不在此限。
        tool_call_id: 关联本次成功的模型工具调用 id；缺省为空字符串。
        display_data: 仅供客户端展示消费的结构化数据；会合并进
            ``ToolObservation.display_data``，不会回传给模型。

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
          的 ``content`` 参数约定）；失败时由 :func:`tool_error` 填入与 ``error``
          相同的错误描述（并非置空），保证模型总能从 ``content`` 读到正文。
        - ``data`` 是「机读字典」：给上层程序逻辑消费的账本（退出码、对象
          类型等），类型恒为 ``dict``；例如删除文件时 ``content`` 写「已删除
          文件 xxx」、``data`` 写 ``{"type": "file", "path": "..."}``，上层
          既能展示文本，也能不解析文本就直接拿到类型/路径做后续判断。

    返回对象的 ``display_data`` 不变量:
        ``display_data`` 恒**不含** ``content`` 键。``content`` 是面向模型的文本，
        下游展示应消费 ``display_data`` 的结构化字段而非其副本；剔除副本可避免大体积
        正文经 ``display_data`` 旁路无约束进入前端事件流与可观测性平台（完整文本只
        存在于 ``observation.content``，并受全局 ``ToolOutputBudget`` 约束）。
    """

    observation = ToolObservation(
        tool_name=tool_name,
        status="success",
        content=content,
        permission=permission,
        tool_call_id=tool_call_id,
    )
    merged_display_data = dataclasses.asdict(observation)
    # 不把完整 content 全文复制进 display_data：content 是面向模型的文本，
    # 下游展示应消费 display_data 的结构化字段而非其副本；避免大体积正文
    # 经 display_data 旁路无约束进入前端事件流与可观测性平台。
    merged_display_data.pop("content", None)
    if data:
        merged_display_data.update(data)
    observation.data = merged_display_data
    return observation
