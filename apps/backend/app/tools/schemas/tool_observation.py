"""工具调用的归一化执行结果（observation）。

本模块只承载一个值对象：``ToolObservation``。它是工具系统对模型/上层
（workflow / runtime）暴露的唯一、稳定的「工具执行结果」数据结构，屏蔽了
子进程隔离、超时强杀、权限校验、参数校验等实现细节——无论成功还是失败，
上层拿到的都是同一个结构。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=False)
class ToolObservation:
    """工具调用产出的归一化观察结果。

    职责边界：
    - 负责：作为工具系统对外（workflow / runtime / 落库）唯一的执行结果载体，
      统一承载「成功内容」与「失败诊断」两类信息。
    - 不负责：不执行任何工具逻辑、不做权限/参数校验、不负责子进程管理与
      强杀；这些都是 :class:`ToolScheduler` / :class:`ToolExecutor` /
      各 handler 的职责，本类只是它们产出的纯数据快照。

    设计要点：
    - 可变快照（``frozen=False``）：字段在构造后可被内部方法修改——唯一例外是
      ``clear_display_data()`` 在把观察转模型消息前清空 ``data``（模型不可见通道）；
      除此之外字段应视为只读，调用方不应改写历史观察。
    - 绝不抛异常：所有失败路径都被归一化为 ``status="error"`` 的观察对象，
      由 :func:`tool_error` 工厂构造，使上层永远拿到可落库/可回传的结果。用户主动
      取消（如父 turn 取消导致子 Agent 中止）则归一化为 ``status="cancelled"``，
      由 :func:`tool_cancelled` 工厂构造——它与 ``error`` 同为确定性终态，但根因是
      「主动中断」而非「执行失败」，须与 ``error`` 明确区分，避免误读为真实故障。

    content 与 data 的区别（易混，单独说明）:
        - ``content`` 是「面向模型的英文人读文本」：给模型/用户看的故事（命令回显、
          文件摘要、可恢复错误等），类型恒为 ``str``；失败时与 ``error`` 同时携带可读
          诊断。它是**模型唯一直接消费的文本通道**。
        - ``data`` 是「面向客户端的结构化机读字典」：给前端渲染消费的计算数据与治理
          标记（如 ``items`` / ``web`` / ``diff`` / ``syntax_errors`` / ``output_truncated`` /
          ``artifact_path``），类型恒为 ``dict``。它**不到模型**——``ToolExecutionService``
          在把观察转模型消息前会 ``clear_display_data()`` 清空，模型只看到
          ``content`` / ``error`` / ``reason``。
        - 两者互不替代、可同时填充：例如删除文件时 ``content`` 写「已删除文件 xxx」，
          ``data`` 写 ``{"type": "file", "path": "..."}``，前端既能展示文本，也能不解析
          文本就直接拿到类型/路径做后续判断；Agent 自修复则只依赖 ``content``/``reason``。

    字段:
        tool_name: 触发本次观察的工具名称（与 :class:`ToolDefinition.name` 对应）。
        status: 执行结果状态，取 ``"success"``、``"error"`` 或 ``"cancelled"``：
            「成功」由 :func:`tool_success` 构造；「失败」由 :func:`tool_error` 构造；
            「取消」由 :func:`tool_cancelled` 构造，表示用户主动中断导致的确定性终态，
            与失败语义不同（根因是主动中止而非执行故障）。
        content: 面向模型/用户的可读正文。成功时为工具输出；失败时为可恢复错误
            说明，并同时通过 ``error`` / ``reason`` 提供结构化诊断。
        error: 失败时回答「发生了什么错误」：面向模型的英文描述，点明失败发生在
            哪个动作及直接的人读原因（如 ``could not write the file: permission
            denied``），**不是**原始异常噪声或堆栈摘要；成功时为空。与 ``reason``
            的分工：``error`` 让模型立刻知道「错在哪一步、直接原因是什么」，
            ``reason`` 进一步给出「为什么发生、该如何修正、是否值得重试」的充足信息。
        reason: 失败时回答「为什么失败、该如何修正、是否值得重试」：面向模型的
            **富文本**说明，**不是**稳定机器短码（``write_failed`` 这类分类码已
            废弃，改在此处写人类可读解释）。内容应包含：①失败根因；②可操作的修正
            建议（模型下一步做什么）；③与 ``retryable`` 一致的重试提示——瞬态失败
            写「用相同参数重试可能成功」，确定性失败写「须先修正参数/路径再调用，
            原样重试必然再次失败」。成功时为空。``retryable`` 是程序化布尔信号，
            本字段是其自然语言补充，两者须保持一致。
        retryable: 回答「原样重试是否可能成功」：``True`` 表示瞬态失败
            （如 ``timeout``、临时文件占用），用相同参数重试有意义；``False``
            表示确定性失败（如参数非法、路径越界），必须先按 ``error`` 中的
            建议修正再调用，原样重试必然再次失败。
        permission: 触发工具所需的权限标识（透传自 :class:`ToolDefinition`），
            便于上层做审计/展示；失败因权限被拒时仍会回填被拒的权限值。
        tool_call_id: 与本次观察对应的模型工具调用 id（透传自 :class:`ToolCall`）；
            用于把观察回绑到具体的模型请求，缺失时为空。
        data: 面向客户端的结构化机读字典（仅前端渲染消费），承载「计算数据 + 治理
            标记」两类：① 工具的结构化结果（如 ``items`` / ``web`` / ``diff`` /
            ``syntax_errors``）；② 输出治理标记（如 ``output_truncated`` /
            ``artifact_path``）。**不到模型**——转模型消息前被 ``clear_display_data()``
            清空；模型只见 ``content`` / ``error`` / ``reason``。不允许承载工具逻辑
            内部数据，其余一律由 content、error、reason 承载。
    """

    # 工具名称：与 ToolDefinition.name 对应，用于上层回绑与审计。
    tool_name: str
    # 执行状态：可取 "success" / "error" / "cancelled"，是上层分流的唯一依据。
    # "cancelled" 表示用户主动中断导致的确定性终态，与失败语义不同。
    status: str
    # 面向模型的可读正文：成功时为工具输出，失败时为可恢复错误说明。
    content: str
    # 「发生了什么错误」：面向模型的英文错误描述（动作+直接原因），
    # 非原始异常噪声；成功时恒为空字符串。
    error: str = ""
    # 「为什么失败、该如何修正」：面向模型的富文本说明（根因+建议+重试提示），
    # 非稳定机器短码；成功时为空。
    reason: str = ""
    # 「原样重试是否可能成功」：瞬态失败为 True；确定性失败为 False，
    # 须先按 error 中的建议修正参数再调用。
    retryable: bool = False
    # 触发工具所需权限标识，透传自 ToolDefinition，便于审计与展示。
    permission: str = ""
    # 对应的模型工具调用 id，透传自 ToolCall，用于observation回绑；缺失为空。
    tool_call_id: str = ""
    # 面向客户端的结构化机读字典（计算数据 + 治理标记），仅前端渲染消费，
    # 不到模型；工具逻辑内部数据一律由 content/error/reason 承载。
    # 类型含 None：clear_display_data() 在转模型消息前会置 None，消费方须容忍 None。
    data: dict[str, Any] | None = field(default_factory=dict)

    def clear_display_data(self) -> None:
        """清空 ``data``（模型不可见通道），供转模型消息前调用。

        历史命名保留 ``clear_display_data``（与 ``display_data`` 时代语义一致），
        未随字段更名 ``data`` 同步，避免破坏 ``ToolExecutionService`` 等调用方。
        """
        self.data = None
