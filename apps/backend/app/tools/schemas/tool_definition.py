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
        """Return a definition with a derived schema when none is supplied.

        派生出的 ``parameters_schema`` 直接复用 :meth:`to_model_tool_definition`
        的产出来源（``args_model.model_json_schema()`` + 本类 name/description），
        保证模型可见 schema 单一事实来源。

        注意：严格化（强制全 ``required``、递归 ``additionalProperties: false``）
        不在本方法或 :meth:`to_model_tool_definition` 中完成，而是完全交由下游的
        ``bind_tools(strict=True)`` 承担；此处只做"投影 + 名称覆盖"，不再重复
        strict 化（避免与 ``bind_tools`` 的内部 ``convert_to_openai_tool(strict=True)``
        重复劳动）。
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
        """Return a model-facing tool definition (name/description/schema projection).

        职责边界（精简后）：
        - 仅做**投影**：用本类契约 ``self.name`` / ``self.description`` 覆盖
          ``args_model.__name__``（如 ``SearchFilesArgs``），保证全链路唯一事实
          来源（registry / tools_node / 前端）一致，不被 pydantic 类名污染。
        - 直接返回 ``args_model.model_json_schema()`` 的原始 JSON schema，**不做
          strict 化**。

        为什么不在本方法内 strict 化：
        - 下游 ``langchain_bridge.model_tools_to_langchain`` 会把本结果交给
          ``bind_tools(tool_schemas, strict=True)``，而 ``bind_tools`` 内部对每个
          tool 再调一次 ``convert_to_openai_tool(..., strict=True)``，强制全
          ``required`` + 递归 ``additionalProperties: false``。若此处也 strict 化，
          属于重复劳动且语义重叠。
        - 因此 strict 化的**唯一事实来源是 ``bind_tools(strict=True)``**，本方法
          保持为"裸 function 投影出口"，不混入严格化策略，职责更单一。

        返回形状：``{"name": str, "description": str, "parameters": dict[str, Any]}``
        （裸 OpenAI function 形状，由 ``bind_tools`` 负责最终包裹为
        ``{"type": "function", "function": {...}}``）。

        参数:
            无。

        返回:
            模型可见工具定义（裸 function 形状），``parameters`` 为 ``args_model``
            的原始 JSON schema（**非** strict 化）。

        异常:
            无（``model_json_schema()`` 对本类构造期已保证合法的 ``args_model`` 不会失败）。

        副作用:
            无（纯投影，不触发 IO 或状态变更）。
        """

        # 名称/描述必须取自本类契约（``self.name`` / ``self.description``），因为
        # 若直接把 pydantic 类交给 ``bind_tools``，LangChain 会用 ``args_model.__name__``
        # （如 ``SearchFilesArgs``）覆盖我们既有的注册名（如 ``search_files``），后者才是
        # registry / tools_node / 前端全链路的唯一事实来源，不可被污染。
        # strict 化不在此处做，交由下游 ``bind_tools(strict=True)`` 统一承担。
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }
