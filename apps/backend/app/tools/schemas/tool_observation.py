"""工具调用的归一化执行结果（observation）。

本模块只承载一个值对象：``ToolObservation``。它是工具系统对模型/上层
（workflow / runtime）暴露的唯一、稳定的「工具执行结果」数据结构，屏蔽了
子进程隔离、超时强杀、权限校验、参数校验等实现细节——无论成功还是失败，
上层拿到的都是同一个结构。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
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
        error: 失败时的机器/人读错误描述（如异常消息、堆栈摘要）；成功时为空。
        reason: 失败分类短码，用于上层区分失败性质，例如
            ``unknown_tool`` / ``permission_denied`` / ``invalid_arguments`` /
            ``handler_exception`` / ``timeout`` 等；成功时为空。
        retryable: 是否可安全重试。当前仅 ``timeout``、``handler_exception``
            等瞬态失败可能被标记为 ``True``，供上层决定是否重放。
        permission: 触发工具所需的权限标识（透传自 :class:`ToolDefinition`），
            便于上层做审计/展示；失败因权限被拒时仍会回填被拒的权限值。
        tool_call_id: 与本次观察对应的模型工具调用 id（透传自 :class:`ToolCall`）；
            用于把观察回绑到具体的模型请求，缺失时为空。
        data: 结构化结果载荷（自由键字典），承载不适合塞进 ``content`` 的
            机器可读字段，例如终端工具的 ``exit_code`` / ``truncated`` /
            ``timed_out``、删除工具的 ``type`` / ``recursive`` 等；成功与失败
            均可能填充。
    """

    # 工具名称：与 ToolDefinition.name 对应，用于上层回绑与审计。
    tool_name: str
    # 执行状态：仅可取 "success" 或 "error"，是上层分流的唯一依据。
    status: str
    # 面向模型的可读正文：成功时为工具输出，失败时为可恢复错误说明。
    content: str
    # 失败时的错误描述（异常消息/堆栈摘要），成功时恒为空字符串。
    error: str = ""
    # 失败分类短码（unknown_tool/permission_denied/invalid_arguments/...），
    # 供上层区分失败性质并决定重试策略；成功时为空。
    reason: str = ""
    # 是否可安全重试：瞬态失败（如 timeout）为 True，供上层重放决策。
    retryable: bool = False
    # 触发工具所需权限标识，透传自 ToolDefinition，便于审计与展示。
    permission: str = ""
    # 对应的模型工具调用 id，透传自 ToolCall，用于observation回绑；缺失为空。
    tool_call_id: str = ""
    # 结构化结果载荷（exit_code/truncated/timed_out/type/recursive 等），
    # 承载机器可读字段；成功与失败均可能填充。
    data: dict[str, Any] = field(default_factory=dict)
