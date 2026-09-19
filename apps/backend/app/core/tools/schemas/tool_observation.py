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
      强杀；这些都是 :class:`ToolExecutor` / :class:`ToolHandlerRunner` /
      各 handler 的职责，本类只是它们产出的纯数据快照。

    设计要点：
    - 可变快照（``frozen=False``）：字段在构造后可被预算治理阶段替换；调用方不应
      改写已经完成的观察。
    - 绝不抛异常：所有失败路径都被归一化为 ``status="error"`` 的观察对象，
      由 :func:`tool_error` 工厂构造，使上层永远拿到可落库/可回传的结果。用户主动
      取消（如父 turn 取消导致子 Agent 中止）则归一化为 ``status="cancelled"``，
      由 :func:`tool_cancelled` 工厂构造——它与 ``error`` 同为确定性终态，但根因是
      「主动中断」而非「执行失败」，须与 ``error`` 明确区分，避免误读为真实故障。

    content、display_data 与 artifact_data 的区别（易混，单独说明）:
        - ``content`` 是「面向模型的英文人读文本」：给模型/用户看的故事（命令回显、
          文件摘要、可恢复错误等），类型恒为 ``str``；失败时与 ``error`` 同时携带可读
          诊断。它是**模型唯一直接消费的文本通道**。
        - ``display_data`` 是「面向客户端的结构化机读字典」：只给前端渲染消费的结果与
          展示治理标记，类型恒为 ``dict``。它不进入模型，也不应被后端持久化逻辑当作事实源。
        - ``artifact_data`` 是工具执行产出的内部产物数据，不进入 Transport，也不到模型，
          例如终端输出超限后落盘的 artifact 路径。文件变更事实由
          不承载文件变更持久化或回退数据。
        - 三者互不替代：Agent 只依赖 ``content`` / ``error`` / ``reason``；UI 只依赖
          ``ToolDisplayHints`` 与 ``display_data``；后端恢复/审计逻辑使用各自明确的持久化
          边界。

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
        reason: 失败时回答「下一步怎么做」：面向模型的**富文本**说明，**不是**稳定
            机器短码（``write_failed`` 这类分类码已废弃，改在此处写人类可读解释）。
            内容应包含可操作的修正或处理建议，并与 ``retryable`` 保持一致：如果
            ``retryable=True``，说明模型按本字段处理后可以再次调用；如果为 False，
            说明应停止、换方案或请求用户介入。成功时为空。``retryable`` 是给模型的
            程序化提示，不是执行器自动重试开关，本字段是其自然语言补充。
        retryable: 回答「按 ``reason`` 修正或处理后，模型是否可以再次调用」：
            ``True`` 表示模型可以修正参数、目标或当前状态后再次调用；``False``
            表示不建议继续重试本工具调用，应停止、换方案或请求用户介入。该字段
            只供模型决策提示使用，不触发执行器自动重试。
        permission: 触发工具所需的权限标识（透传自 :class:`ToolDefinition`），
            便于上层做审计/展示；失败因权限被拒时仍会回填被拒的权限值。
        tool_call_id: 与本次观察对应的模型工具调用 id（透传自 :class:`ToolCall`）；
            用于把观察回绑到具体的模型请求，缺失时为空。
        display_data: 面向客户端的结构化机读字典，仅供前端渲染。UI 不应通过本字段之外的
            Observation 字段推导展示结果。
        artifact_data: 工具执行产出的内部产物数据；不进入事件、快照或模型上下文。
            文件展示数据不代表持久化的变更事实。
    """

    # 工具名称：与 ToolDefinition.name 对应，用于上层回绑与审计。
    tool_name: str
    # 执行状态：可取 "success" / "error" / "cancelled"，是上层分流的唯一依据。
    # "cancelled" 表示用户主动中断导致的确定性终态，与失败语义不同。
    status: str
    # 面向模型的可读正文：成功时为工具输出，失败时为可恢复错误说明。
    content: str | None = None
    # 「发生了什么错误」：面向模型的英文错误描述（动作+直接原因），
    # 非原始异常噪声；成功时恒为空字符串。
    error: str | None = None
    # 「为什么失败、该如何修正」：面向模型的富文本说明（根因+建议+重试提示），
    # 非稳定机器短码；成功时为空。
    reason: str | None = None
    # 「按 reason 修正或处理后，模型是否可以再次调用」；仅供模型决策提示使用，
    # 不触发执行器自动重试。
    retryable: bool = False
    # 触发工具所需权限标识，透传自 ToolDefinition，便于审计与展示。
    permission: str | None = ""
    # 对应的模型工具调用 id，透传自 ToolCall，用于observation回绑；缺失为空。
    tool_call_id: str = ""
    # 面向客户端的结构化机读字典，仅前端渲染消费，不到模型。
    display_data: dict[str, Any] | None = field(default_factory=dict)
    # 工具执行产出的内部结构化事实（产物数据）；不进入 Transport 或模型上下文。
    artifact_data: dict[str, Any] | None = field(default_factory=dict)
