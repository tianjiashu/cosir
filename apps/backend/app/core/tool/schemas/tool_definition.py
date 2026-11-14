"""工具定义及模型可见契约。"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import BaseModel

from app.core.tool.call_intent import model_parameters_with_intent
from app.core.tool.presentation import ToolPresentation
from app.core.tool.schemas.decision_payload import DecisionPayload
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema


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
    # 呈现类别是工具语义；文案、图标与展开状态归前端，不进入模型 schema。
    conversation_presentation: ToolPresentation = "activity"
    # 隔离执行模式（仅 executor 读取，不进模型可见结构）："thread"=当前线程直跑
    # handler，无子进程/Queue/pickle/日志桥、无硬超时强杀；"process"=子进程隔离 +
    # 硬超时强杀（terminate→kill→进程组/Job Object 树杀）+ 跨进程日志桥。
    execution_mode: Literal["thread", "process"] = "thread"
    # 调度字段：与 execution_mode 正交（前者选隔离方式，本字段选串行/并行调度）。
    parallel_mode: Literal["serial", "parallel"] = "serial"
    # 需要用户决定（human in the loop）：首次调用只登记待决事项并返回等待占位
    # （:func:`tool_awaiting_decision`），用户决定之后的去向由这里声明，不看决定本身：
    # ``"execute"`` 批准或驳回后都由 prepare_input 绕过模型、带着决定再次执行同一调用（如交接：
    # 用户交还即作出决定，重放时取回登记并报告资源状态，执行结果让等待占位收尾）；
    # ``"reply"`` 用户的回答作为消息交给模型（如意图澄清）；``None`` 不需要用户决定。
    # 非 None 的工具一批里只能单独调用（见 ``tool_batch_runner``），呈现类别必须是 ``standalone``：
    # 等待时前端在调用的原位置画成卡片，收在工具组里会被折叠的组藏住（注册表守卫核对）。
    user_decision: Literal["execute", "reply"] | None = None
    # 决定携带的结构化载荷（见 :class:`DecisionPayload`）；``None`` 表示决定只有批准 / 驳回与附言。
    # 只对 ``reply`` 类有意义：载荷渲染成给模型的正文，并进等待占位的展示数据。
    decision_model: type[DecisionPayload] | None = None
    # 等待中的决定何时作废：``"turn"`` 只在发起它的回合内有效——并入一条开启新回合的输入、或同类
    # 的新等待写下时作废（提问问的是「此刻做不做」，见 ``turn_decision_expiry``）；``"open"`` 一直
    # 等到用户决定（交接要等到交还）。两者都随撤回所在回合、Session 删除结束。
    decision_lifetime: Literal["turn", "open"] = "open"
    # 产出图片给模型看（观察的 ``images``）：本次 Run 的模型不支持图片输入时，准入门禁直接拒绝，
    # 不进 handler——判定只在 ``ToolAccessGate`` 一处，各工具不再各写一遍。
    returns_images: bool = False
    # 要在用户能操作的界面里才用得了（交互终端、配置草稿）：只给侧栏会话。需要用户作答的工具
    # （``user_decision`` 非空）天然如此，不必重复声明，见 :attr:`requires_user_surface`。
    needs_user_surface: bool = False

    @property
    def requires_user_surface(self) -> bool:
        """是否只能给用户能操作的 Session（``models.session_kind.SessionKind.user_facing``）。"""
        return self.needs_user_surface or self.user_decision is not None

    def normalized(self) -> "ToolDefinition":
        """注册时固定 strict 参数 schema，模型调用阶段不再转换。

        显式 schema 来自平台投影；其余由统一出口生成。
        """
        if self.parameters_schema:
            return self
        # ``replace`` 而不是逐字段重建：逐字段写法曾漏掉 ``user_decision``，注册后的 HITL 工具
        # 因此不再被 prepare_input 识别为需要执行的决定。
        return replace(self, parameters_schema=strict_parameters_schema(self.args_model))

    def to_model_tool_definition(self) -> dict[str, Any]:
        """导出可固化到 Session 的 strict function 定义。

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
            "parameters": model_parameters_with_intent(
                self.parameters_schema or strict_parameters_schema(self.args_model)
            ),
        }
