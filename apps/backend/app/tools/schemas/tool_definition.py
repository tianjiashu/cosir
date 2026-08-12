"""Tool definition value object."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from app.tools.schemas.tool_display import ToolDisplayHints


@dataclass(frozen=True)
class ToolDefinition:
    """可执行工具的元数据与 handler 契约（工具系统的单一事实来源）。

    单一职责：承载一个工具的完整元数据与执行契约，作为工具系统对内对外
    的「单一事实来源」。模型可见结构（name / description / parameters）由
    :meth:`to_model_tool_definition` 投影，内部契约字段（permission /
    resource_keys / execution_mode 等）不进模型可见结构，仅由对应消费模块读取。

    设计边界：
    -     负责：声明工具身份（name）、面向模型的描述与参数 schema、绑定的执行
      handler、参数校验契约（args_model，强制必填，为工具入参唯一校验入口）、
      以及供各子系统协调消费的元数据（permission / risk_level /
      resource_keys / execution_mode / display / visible_by_default / timeout_seconds）。
    - 不负责：工具实际执行（ToolExecutor / ToolScheduler）、权限校验
      （ToolScheduler 的 allowed_tool_names / 审批系统）、参数校验
      （validation.arguments）、模型可见性策略投影之外的编排逻辑。本类是纯
      数据契约，不持有任何执行或调度逻辑。

    关键字段语义：
    - ``args_model``：参数校验契约（pydantic 模型），**强制必填**，是工具入参
      唯一校验入口；``to_model_tool_definition`` 与 ``normalized`` 也据此投影/派生
      模型可见 schema。不再保留并列的 ``required_params`` 冗余声明。
    - ``execution_mode``：隔离执行模式，仅 ``ToolExecutor`` 读取，不进模型可见
      结构；``"process"`` 走子进程隔离 + 硬超时强杀 + 树杀，``"thread"``（默认）
      在当前线程直跑、无子进程开销但无硬超时强杀。该字段按「是否需要 OS 级故障
      隔离」逐工具声明，不按「是否文件工具」归类。
    - ``resource_keys``：资源协调键序列，声明本工具触碰的资源（如
      ``("filesystem",)`` / ``("shell",)``），供资源级授权、分组展示等子系统
      协调消费，是「触碰何种资源」的单一事实来源；注意它与 ``execution_mode``
      （如何隔离）正交，也与未来可能的来源字段（builtin / mcp）正交，**不接管
      路径边界等安全强制**——安全网由各 handler / ProjectPathResolver 硬编码保障。
    - ``to_model_tool_definition`` 仅投影 ``name`` / ``description`` /
      ``parameters``，不会泄漏 ``permission`` / ``resource_keys`` /
      ``execution_mode`` 等内部契约。

    参数:
        见下方各字段定义（frozen dataclass，构造即不可变）。

    返回:
        无（类定义，无返回值）。

    异常:
        无（字段校验交由消费模块在运行时完成）。

    副作用:
        无（纯不可变数据对象，不触发 IO 或状态变更）。
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
    # ToolExecutionService consumes these scheduling fields. They are orthogonal to
    # execution_mode: execution_mode chooses isolation, parallel_mode chooses batching.
    parallel_mode: Literal["serial", "parallel"] = "serial"
    parallel_group: str = "default"

    def normalized(self) -> "ToolDefinition":
        """Return a definition with a derived schema when none is supplied."""

        if self.parameters_schema:
            return self
        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.handler,
            parameters_schema=self.args_model.model_json_schema(),
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
        """Return a model-facing tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }
