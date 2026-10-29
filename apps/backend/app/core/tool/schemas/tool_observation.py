"""工具调用的归一化执行结果（observation）。

本模块只承载一个值对象：``ToolObservation``。它是工具系统对模型/上层
（workflow / runtime）暴露的唯一、稳定的「工具执行结果」数据结构，屏蔽了
子进程隔离、超时强杀、权限校验、参数校验等实现细节——无论成功还是失败，
上层拿到的都是同一个结构。
"""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=False)
class ToolObservation:
    """工具调用产出的归一化观察结果。

    职责边界：
    - 负责：作为工具系统对外（workflow / runtime / 落库）唯一的执行结果载体。
    - 不负责：不执行任何工具逻辑、不做权限/参数校验、不负责子进程管理与
      强杀；这些都是 :class:`ToolExecutor` / :class:`ToolHandlerRunner` /
      各 handler 的职责，本类只是它们产出的纯数据快照。

    设计要点：
    - 可变快照（``frozen=False``）：字段在构造后可被预算治理阶段替换；调用方不应
      改写已经完成的观察。
    - 绝不抛异常：失败路径归一化为 ``status="error"``（:func:`tool_error`），主动取消
      归一化为 ``status="cancelled"``（:func:`tool_cancelled`），上层永远拿到可落库/可回传
      的结果。
    - 结构参照 MCP ``CallToolResult``：``status`` 对应 ``isError``，模型可见的一切（成功输出、
      错误事实与处理建议、取消说明）都在 ``content`` 一个文本通道里，由构造工厂在源头
      组织成最终文本；workflow 不再从多个诊断字段拼装模型消息。

    content、display_data 与 artifact_data 的区别（易混，单独说明）:
        - ``content`` 是模型唯一直接消费的英文文本通道。
        - ``display_data`` 是只给前端渲染消费的结构化字典，不进入模型，也不应被后端
          持久化逻辑当作事实源。
        - ``artifact_data`` 是工具执行产出的内部产物数据（如终端输出超限后落盘的路径），
          不进入 Transport，也不到模型。

    字段:
        tool_name: 触发本次观察的工具名称（与 :class:`ToolDefinition.name` 对应）。
        status: ``"success"`` / ``"error"`` / ``"cancelled"``，是上层分流的唯一依据；
            ``cancelled`` 表示主动中断导致的确定性终态，与执行故障区分。
        content: 模型可见正文。成功时为工具输出（可为空，由 workflow 发 ``success``）；
            失败与取消时为构造工厂组织好的说明文本。
        tool_call_id: 对应的模型工具调用 id（透传自 :class:`ToolCall`），缺失时为空。
        display_data: 面向客户端的结构化机读字典，仅供前端渲染。
        team_run_id: 已创建 TeamRun 的定位信息；与错误短提示分离，供 Transport 在工具失败
            后继续查询持久化执行事实，不代表第二套 Team 状态。
        suspend: 请求 Agent 挂起等消息：本批观察写入后由 prepare_input 迁移 Run 为
            ``suspended``、不再请求模型，由可唤醒消息继续同一个 Run（取消优先；已有外部可唤醒
            消息时直接继续）。它是工作流控制流事实，不进模型上下文、不随 ``display_data`` 下发。
        artifact_data: 工具执行产出的内部产物数据；不进入事件、快照或模型上下文。
        awaiting_decision: 本观察是 HITL 工具的等待占位（见 :func:`tool_awaiting_decision`）：模型
            协议上它就是这次调用的最终结果，用户决定后的执行结果另行追加为消息、不覆盖它；投影为
            非终态 ``pending``，执行完成后只更新展示状态。
        deferred: 本观察是延迟结果的后台占位（结论稍后以 ``background_result`` 回投，见
            ``core/tool/background``）：与 HITL 占位一样投影为 ``pending``，结论并入或撤销时只更新
            占位行的展示状态；持久化为占位行 metadata 的 ``deferred``，卡片据此显示「后台执行中」。
    """

    tool_name: str
    status: Literal["success", "error", "cancelled"]
    content: str | None = None
    tool_call_id: str = ""
    display_data: dict[str, Any] | None = field(default_factory=dict)
    team_run_id: int | None = None
    suspend: bool = False
    artifact_data: dict[str, Any] | None = field(default_factory=dict)
    awaiting_decision: bool = False
    deferred: bool = False
