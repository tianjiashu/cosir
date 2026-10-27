"""工具定义及模型可见契约。"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import BaseModel

from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.tool.schemas.tool_display import ToolDisplayHints


@dataclass(frozen=True)
class ToolDefinition:
    """工具系统的元数据与 handler 契约单一事实来源（frozen）。

    模型可见的 strict function 定义由 ``to_model_tool_definition``
    投影；内部契约字段（resource_keys / execution_mode 等）不进模型
    可见结构，仅由对应消费模块读取。本类纯数据契约，不持有任何执行或调度逻辑。

    关键字段：
    - ``args_model``：参数校验契约（pydantic 模型），强制必填，是工具入参唯一校验
      入口；``to_model_tool_definition`` 与 ``normalized`` 据此投影模型可见 schema。
    - ``execution_mode``：隔离执行模式，仅 ``ToolHandlerRunner`` 读取。``"process"`` 走
      子进程隔离 + 硬超时强杀 + 树杀，``"thread"``（默认）线程直跑、无子进程开销但
      无硬超时强杀；按「是否需要 OS 级故障隔离」逐工具声明，不按「是否文件工具」归类。
    - ``resource_keys``：本工具触碰的资源（如 ``("filesystem",)`` / ``("shell",)``），
      供资源级授权、分组展示协调消费，与 ``execution_mode``（如何隔离）正交，**不接管
      路径边界等安全强制**（由各 handler / ProjectPathResolver 保障）。
    """

    name: str
    group: str
    description: str
    handler: Callable[..., Any]
    args_model: type[BaseModel]
    parameters_schema: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    resource_keys: Sequence[str] = field(default_factory=tuple)
    display: ToolDisplayHints | None = None
    # 隔离执行模式（仅 executor 读取，不进模型可见结构）："thread"=当前线程直跑
    # handler，无子进程/Queue/pickle/日志桥、无硬超时强杀；"process"=子进程隔离 +
    # 硬超时强杀（terminate→kill→进程组/Job Object 树杀）+ 跨进程日志桥。
    execution_mode: Literal["thread", "process"] = "thread"
    # 调度字段：与 execution_mode 正交（前者选隔离方式，本字段选串行/并行调度）。
    parallel_mode: Literal["serial", "parallel"] = "serial"
    # 需要用户决定（human in the loop）：首次调用只登记待决事项并返回等待占位
    # （:func:`tool_awaiting_decision`），用户决定之后的去向由这里声明，不看决定本身：
    # ``"execute"`` 批准或驳回后都由 prepare_input 绕过模型、带着决定再次执行同一调用（如 Agent
    # Team：批准即启动、驳回即落定，执行结果让等待占位收尾）；
    # ``"reply"`` 用户的回答作为消息交给模型（如意图澄清）；``None`` 不需要用户决定。
    # 非 None 的工具一批里只能单独调用（见 ``tool_batch_runner``），每个 Task 同时最多一个等待中的
    # 调用由工具自己的事实源保证。
    user_decision: Literal["execute", "reply"] | None = None
    # 批准时随决定提交的载荷（``DecisionBody.data``）的形状，与 ``args_model`` 对称：决定入口按它
    # 校验前端输入，不合法同步拒绝；``None`` 表示决定不带载荷。只对 ``user_decision`` 非 None 的工具
    # 有意义。
    decision_model: type[BaseModel] | None = None

    def normalized(self) -> "ToolDefinition":
        """注册时固定 strict 参数 schema，模型调用阶段不再转换。

        显式 schema 由平台投影从 strict_parameters_schema 生成；其余由统一出口生成。
        """
        if self.parameters_schema:
            return self
        # ``replace`` 而不是逐字段重建：逐字段写法曾漏掉 ``user_decision``，注册后的 HITL 工具
        # 因此不再被 prepare_input 识别为需要执行的决定。
        return replace(self, parameters_schema=self.to_model_tool_definition()["parameters"])

    def to_model_tool_definition(self) -> dict[str, Any]:
        """导出可固化到 Task 的 strict function 定义。

        仅做投影：用本类 ``name`` / ``description`` 覆盖 pydantic 类名（如
        ``SearchContentArgs``），确保 registry / tools_node / 前端全链路使用注册名
        （如 ``search_content``）这一唯一事实来源，不被 pydantic 类名污染。

        参数 schema 使用显式 strict 投影；未显式提供时复用 SDK 从 ``args_model`` 生成。
        模型须显式填写所有字段；本地业务约束与默认值仍由工具参数入口处理。

        返回含 ``strict=True`` 的裸 function 形状，由
        SDK 适配器包裹为 ``{"type": "function", "function": {...}}``。
        """
        # 名称/描述取本类契约而非 args_model.__name__，避免 pydantic 类名
        # 覆盖注册名，污染全链路唯一事实来源。
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "parameters": self.parameters_schema or strict_parameters_schema(self.args_model),
        }
