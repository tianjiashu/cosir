"""Tool definition value object."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from app.tools.schemas.tool_display import ToolDisplayHints


@dataclass(frozen=True)
class ToolDefinition:
    """Executable tool metadata and handler contract."""

    name: str
    description: str
    permission: str
    required_params: Iterable[str]
    handler: Callable[..., Any]
    parameters_schema: Mapping[str, Any] = field(default_factory=dict)
    args_model: type[BaseModel] | None = None
    timeout_seconds: float = 10.0
    risk_level: str = "low"
    visible_by_default: bool = True
    resource_keys: Sequence[str] = field(default_factory=tuple)
    display: ToolDisplayHints | None = None
    # 隔离执行模式（仅 executor 读取，不进模型可见结构）："thread"=当前线程直跑
    # handler，无子进程/Queue/pickle/日志桥、无硬超时强杀；"process"=子进程隔离 +
    # 硬超时强杀（terminate→kill→进程组/Job Object 树杀）+ 跨进程日志桥。
    execution_mode: Literal["thread", "process"] = "thread"

    def normalized(self) -> "ToolDefinition":
        """Return a definition with a derived schema when args_model is provided."""

        if self.parameters_schema or self.args_model is None:
            return self
        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            required_params=self.required_params,
            handler=self.handler,
            parameters_schema=self.args_model.model_json_schema(),
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            visible_by_default=self.visible_by_default,
            resource_keys=self.resource_keys,
            display=self.display,
            execution_mode=self.execution_mode,
        )

    def to_model_tool_definition(self) -> dict[str, Any]:
        """Return a model-facing tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters_schema),
        }
