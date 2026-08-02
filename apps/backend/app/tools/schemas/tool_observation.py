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
    - 不可变（``frozen=True``）：结果一旦构造即不可更改，避免上层误改历史
      观察，也利于跨进程/跨线程安全传递。
    - 绝不抛异常：所有失败路径都被归一化为 ``status="error"`` 的观察对象，
      由 :func:`tool_error` 工厂构造，使上层永远拿到可落库/可回传的结果。

    content 与 data 的区别（易混，单独说明）:
        - ``content`` 是「人读文本」：给模型/用户看的故事（命令回显、文件
          摘要、可恢复错误等），类型恒为 ``str``；失败时与 ``error`` 同时
          携带可读诊断，``reason`` 提供稳定机器分类。
        - ``data`` 是「机读字典」：给上层程序逻辑消费的账本（``exit_code`` /
          ``type`` / ``recursive`` 等结构化字段），类型恒为 ``dict``。
        - 两者互不替代、可同时填充：例如删除文件时 ``content`` 写「已删除
          文件 xxx」，``data`` 写 ``{"type": "file", "path": "..."}``，上层
          既能展示文本，也能不解析文本就直接拿到类型/路径做后续判断。

    字段:
        tool_name: 触发本次观察的工具名称（与 :class:`ToolDefinition.name` 对应）。
        status: 执行结果状态，仅取 ``"success"`` 或 ``"error"`` 两个值。
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
        data: 只允许承载客户端渲染所需的计算数据，不允许承载工具逻辑的数据；
            其他一律由 content、error、reason 承载。
    """

    # 工具名称：与 ToolDefinition.name 对应，用于上层回绑与审计。
    tool_name: str
    # 执行状态：仅可取 "success" 或 "error"，是上层分流的唯一依据。
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
    # 只允许承载客户端渲染所需的计算数据，不允许承载工具逻辑的数据；
    # 其他一律由 content、error、reason 承载。
    display_data: dict[str, Any] = field(default_factory=dict)

    # display_data 不可以给模型看，用完后要清空
    def clear_display_data(self):
        self.display_data = None
