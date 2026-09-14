"""Tool definition value object."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from app.core.tools.schemas.tool_display import ToolDisplayHints


@dataclass(frozen=True)
class ToolDefinition:
    """工具系统的元数据与 handler 契约单一事实来源（frozen）。

    模型可见结构（name / description / parameters）由 ``to_model_tool_definition``
    投影；内部契约字段（permission / resource_keys / execution_mode 等）不进模型
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
    description: str
    permission: str
    handler: Callable[..., Any]
    args_model: type[BaseModel]
    parameters_schema: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    risk_level: str = "low"
    resource_keys: Sequence[str] = field(default_factory=tuple)
    display: ToolDisplayHints | None = None
    # 隔离执行模式（仅 executor 读取，不进模型可见结构）："thread"=当前线程直跑
    # handler，无子进程/Queue/pickle/日志桥、无硬超时强杀；"process"=子进程隔离 +
    # 硬超时强杀（terminate→kill→进程组/Job Object 树杀）+ 跨进程日志桥。
    execution_mode: Literal["thread", "process"] = "thread"
    # 调度字段：与 execution_mode 正交（前者选隔离方式，本字段选批处理分组）。
    parallel_mode: Literal["serial", "parallel"] = "serial"
    parallel_group: str = "default"

    def normalized(self) -> "ToolDefinition":
        """Return a copy with a derived schema when none was supplied.

        无 ``parameters_schema`` 时复用 ``to_model_tool_definition`` 的产出
        （``args_model.model_json_schema()`` + 本类 name/description），保证模型可见
        schema 单一事实来源。strict 化不在此处做，统一由下游 ``bind_tools(strict=True)``
        承担，避免重复劳动。
        """
        if self.parameters_schema:
            return self
        model_def = self.to_model_tool_definition()
        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.handler,
            parameters_schema=model_def["parameters"],
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=self.resource_keys,
            display=self.display,
            execution_mode=self.execution_mode,
            parallel_mode=self.parallel_mode,
            parallel_group=self.parallel_group,
        )

    def to_model_tool_definition(self) -> dict[str, Any]:
        """Project to the model-facing tool definition (name/description/schema).

        仅做投影：用本类 ``name`` / ``description`` 覆盖 pydantic 类名（如
        ``SearchContentArgs``），确保 registry / tools_node / 前端全链路使用注册名
        （如 ``search_content``）这一唯一事实来源，不被 pydantic 类名污染。直接返回
        ``parameters_schema``（存在时）或 ``args_model.model_json_schema()`` 的 schema，
        **不做 strict 化**（交由下游 ``bind_tools(strict=True)`` 统一承担）。

        返回 ``{"name", "description", "parameters"}``（裸 function 形状，由
        ``bind_tools`` 包裹为 ``{"type": "function", "function": {...}}``）。
        """
        # 名称/描述取本类契约而非 args_model.__name__，避免 LangChain 用 pydantic 类名
        # 覆盖注册名，污染全链路唯一事实来源。
        parameters = self.parameters_schema or self.args_model.model_json_schema()
        return {
            "name": self.name,
            "description": self.description,
            "parameters": parameters,
        }
