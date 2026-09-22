"""Tool definition value object."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from app.config.logging.logger import log
from app.core.tools.schemas.tool_display import ToolDisplayHints


@runtime_checkable
class AsyncToolHandler(Protocol):
    """Explicit contract for handlers that must execute on the backend event loop."""

    def __call__(self, **kwargs: Any) -> Awaitable[Any]:
        """Return an awaitable tool result without blocking the event loop."""
        ...


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
    # Handler dispatch is explicit.  Existing built-in handlers remain sync by default;
    # async handlers must opt in and are validated by ToolRegistry at registration.
    handler_kind: Literal["sync", "async"] = "sync"
    # 运行期投影钩子（可选）：非 None 时，模型可见描述 / 参数 schema 在**每次投影时**
    # 实时生成，不在注册期快照固化。用于内容依赖运行期单例（如 agent 注册表）而启动期
    # 尚不可用的工具——否则注册期取到的空值会被永久固化，模型永远拿不到真实候选集
    # （2026-09-17 delegate_task 委派全线失败即此因）。显式传入的 description /
    # parameters_schema 仍为最高优先级的静态兜底。compare=False：钩子是行为而非身份，
    # 不参与定义相等性判断。
    description_provider: Callable[[], str] | None = field(default=None, compare=False)
    schema_provider: Callable[[], Mapping[str, Any]] | None = field(default=None, compare=False)

    def normalized(self) -> "ToolDefinition":
        """Return a copy with a derived schema when none was supplied.

        无 ``parameters_schema`` 且未声明 ``schema_provider`` 时，复用
        ``to_model_tool_definition`` 的产出（``args_model.model_json_schema()`` +
        本类 name/description），保证模型可见 schema 单一事实来源。声明了
        ``schema_provider`` 的工具**不固化** schema，留给每次投影实时生成。
        strict 化不在此处做，统一由下游 ``bind_tools(strict=True)`` 承担，避免重复劳动。
        """
        if self.parameters_schema or self.schema_provider is not None:
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
            handler_kind=self.handler_kind,
            description_provider=self.description_provider,
            schema_provider=self.schema_provider,
        )

    def to_model_tool_definition(self) -> dict[str, Any]:
        """Project to the model-facing tool definition (name/description/schema).

        仅做投影：用本类 ``name`` / ``description`` 覆盖 pydantic 类名（如
        ``SearchContentArgs``），确保 registry / tools_node / 前端全链路使用注册名
        （如 ``search_content``）这一唯一事实来源，不被 pydantic 类名污染。

        取值与兜底统一收口在 ``project_description`` / ``project_parameters``（静态值
        优先，运行期钩子次之，钩子异常一律降级），本方法只做组合投影。**不做 strict
        化**（交由下游 ``bind_tools(strict=True)`` 统一承担）。

        返回 ``{"name", "description", "parameters"}``（裸 function 形状，由
        ``bind_tools`` 包裹为 ``{"type": "function", "function": {...}}``）。
        """
        # 名称/描述取本类契约而非 args_model.__name__，避免 LangChain 用 pydantic 类名
        # 覆盖注册名，污染全链路唯一事实来源。
        return {
            "name": self.name,
            "description": self.project_description(),
            "parameters": self.project_parameters(),
        }

    def project_description(self) -> str:
        """投影面向模型的工具描述：运行期钩子优先，钩子缺失或失败时回退静态值。

        与 ``project_parameters`` 的优先级**刻意不同**：描述的场景是「静态文本只是兜底，
        真实内容要运行时才知道」，而 schema 的场景是「静态显式 schema 是权威覆盖」。两者
        的不对称是有意设计，勿强行统一。

        投影位于「每次下发模型」的热路径上（``workflow`` 每次 run 都会遍历工具定义投影），
        因此钩子异常必须在本方法内收口并降级为静态 ``description``——绝不让单个工具的
        钩子故障炸穿整轮 run。

        参数:
            无。

        返回:
            ``description_provider`` 存在且成功时取其结果，否则取静态 ``description``。

        异常:
            无（钩子异常已收口）。

        副作用:
            钩子抛异常时写一条 WARNING 日志（``tool_description_provider_failed``），
            含工具名与异常类型，便于定位而不泄密。
        """

        if self.description_provider is None:
            return self.description
        try:
            return self.description_provider()
        except Exception as exc:
            log.warning(
                "tool_description_provider_failed",
                extra={
                    "msg": "工具描述运行期投影失败，已降级为静态描述",
                    "data": {"tool": self.name, "error_type": type(exc).__name__},
                },
            )
            return self.description

    def project_parameters(self) -> Mapping[str, Any]:
        """投影面向模型的参数 schema：静态 > 运行期钩子 > ``args_model``。

        同为模型下发热路径：``parameters_schema`` 为空且钩子异常时降级为
        ``args_model.model_json_schema()``（工具自身的静态契约，仍属最小可用），避免异常
        穿透到模型下发点。

        参数:
            无。

        返回:
            参数 JSON schema 映射。

        异常:
            无（钩子异常已收口；``args_model`` 自身的异常属工具定义错误，向上暴露）。

        副作用:
            钩子抛异常时写一条 WARNING 日志（``tool_schema_provider_failed``）。
        """

        if self.parameters_schema:
            return self.parameters_schema
        if self.schema_provider is not None:
            try:
                return self.schema_provider()
            except Exception as exc:
                log.warning(
                    "tool_schema_provider_failed",
                    extra={
                        "msg": "工具参数 schema 运行期投影失败，已降级为 args_model 契约",
                        "data": {"tool": self.name, "error_type": type(exc).__name__},
                    },
                )
        return self.args_model.model_json_schema()
