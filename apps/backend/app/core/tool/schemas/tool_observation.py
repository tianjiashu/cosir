"""工具调用的归一化执行结果（observation）。

本模块只承载一个值对象：``ToolObservation``。它是工具系统对模型/上层
（workflow / runtime）暴露的唯一、稳定的「工具执行结果」数据结构，屏蔽了
子进程隔离、超时强杀、权限校验、参数校验等实现细节——无论成功还是失败，
上层拿到的都是同一个结构。
"""

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import Field

from app.models.message_context.message_metadata.tool_message_metadata import (
    BackgroundWork,
    ToolResultFacts,
)


@dataclass(frozen=True)
class AwaitDecision:
    """HITL 等待占位：等用户决定，Agent 挂起。"""

    kind: Literal["decision"] = "decision"


@dataclass(frozen=True)
class RunInBackground:
    """后台占位：工作继续，结论稍后回投；Agent 不挂起。"""

    work: BackgroundWork
    kind: Literal["background"] = "background"


@dataclass(frozen=True)
class AwaitMessages:
    """``wait_messages``：调用已完成，Agent 挂起等消息。"""

    kind: Literal["messages"] = "messages"


# ``kind`` 是判别键：批次随执行状态落库（``ToolBatch``），读回时据它还原成哪一种。
Waiting = Annotated[AwaitDecision | RunInBackground | AwaitMessages, Field(discriminator="kind")]


@dataclass(frozen=False)
class ToolObservation:
    """工具调用产出的归一化观察结果。

    职责边界：
    - 负责：作为工具系统对外（workflow / runtime / 落库）唯一的执行结果载体。
    - 不负责：不执行任何工具逻辑、不做权限/参数校验；这些都是 :class:`ToolExecutor` /
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

    content 与 display_data 的区别（易混，单独说明）:
        - ``content`` 是模型唯一直接消费的英文文本通道；超出预算时截断，完整原文落盘的路径写在截断
          标记里（``ToolOutputBudget``）。
        - ``display_data`` 是只给前端渲染消费的结构化字典，不进入模型，也不应被后端
          持久化逻辑当作事实源。

    字段:
        tool_name: 触发本次观察的工具名称（与 :class:`ToolDefinition.name` 对应）。
        status: ``"success"`` / ``"error"`` / ``"cancelled"``，是上层分流的唯一依据；
            ``cancelled`` 表示主动中断导致的确定性终态，与执行故障区分。
        content: 模型可见正文。成功时为工具输出（可为空，由 workflow 发 ``success``）；
            失败与取消时为构造工厂组织好的说明文本。
        tool_call_id: 对应的模型工具调用 id（透传自 :class:`ToolCall`），缺失时为空。
        display_data: 面向客户端的结构化机读字典，仅供前端渲染。
        waiting: 这次调用之后还要等什么，``None`` 即普通结果。三种情形互斥，用一个判别联合表达，
            「等决定」同时又是后台占位这类组合在类型上写不出来：

            - :class:`AwaitDecision`：HITL 工具的等待占位（见 :func:`tool_awaiting_decision`）。模型协议上
              它就是这次调用的最终结果，用户决定后的执行结果另行追加为消息、不覆盖它；投影为非终态
              ``pending``，执行完成后只更新展示状态。Agent 挂起。
            - :class:`RunInBackground`：后台占位，结论稍后以 ``background_result`` 回投（见
              ``tools/tool_handler/terminal/command_results``）。同样投影为 ``pending``，工作记录原样
              持久化为占位行 metadata 的 ``background``，卡片与看板据此显示；开始时间由
              ``CommandResultRegistry.register`` 登记时确定。Agent 不挂起：后台工作属于 Session。
            - :class:`AwaitMessages`：``wait_messages`` 要 Agent 挂起等消息，调用本身已经完成。

            挂起是工作流控制流事实（:attr:`suspend`）：本批观察写入后由 prepare_input 迁移 Run 为
            ``suspended``、不再请求模型，由可唤醒消息继续同一个 Run；不进模型上下文、不随
            ``display_data`` 下发。
        images: 工具产出给模型看的图片（工作区附件的相对路径，见
            ``AttachmentService.save_image``）。
            tool 消息只能是文本，图片由 observe 节点在整批 tool 消息之后以一条 ``tool_image`` user
            消息直接写进 context（见 ``write_tool_images``）；不进 ``display_data``。
    """

    tool_name: str
    status: Literal["success", "error", "cancelled"]
    content: str | None = None
    tool_call_id: str = ""
    display_data: dict[str, Any] | None = field(default_factory=dict)
    waiting: Waiting | None = None
    images: tuple[str, ...] = ()
    # 交给运行时的结构化结果（交付记录、加载的外部工具等）：原样持久化为结果行 metadata 的 ``facts``。
    facts: ToolResultFacts | None = None

    @property
    def suspend(self) -> bool:
        """调用结束后 Agent 应挂起等消息：等用户决定或显式等消息；后台占位不挂起。"""
        return isinstance(self.waiting, AwaitDecision | AwaitMessages)

    @property
    def awaiting_decision(self) -> bool:
        return isinstance(self.waiting, AwaitDecision)

    @property
    def background(self) -> BackgroundWork | None:
        return self.waiting.work if isinstance(self.waiting, RunInBackground) else None
