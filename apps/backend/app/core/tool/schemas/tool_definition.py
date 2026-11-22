"""工具定义及模型可见契约。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel

from app.core.tool.presentation import ToolPresentation
from app.core.tool.schemas.decision_payload import DecisionPayload
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema


@dataclass(frozen=True)
class ToolDefinition:
    """工具系统的元数据与 handler 契约单一事实来源（frozen）。

    模型可见的 strict function 定义由 ``to_model_tool_definition``
    投影；内部契约字段不进模型
    可见结构，仅由对应消费模块读取。本类纯数据契约，不持有任何执行或调度逻辑。

    关键字段：
    - ``args_model``：参数校验契约（pydantic 模型），强制必填，是工具入参唯一校验
      入口；``to_model_tool_definition`` 与 ``normalized`` 据此投影模型可见 schema。
    """

    name: str
    group: str
    description: str
    handler: Callable[..., Any]
    args_model: type[BaseModel]
    parameters_schema: Mapping[str, Any] = field(default_factory=dict)
    # 呈现类别是工具语义；文案、图标与展开状态归前端，不进入模型 schema。
    conversation_presentation: ToolPresentation = "activity"
    # 需要用户决定（human in the loop，如意图澄清）：首次调用只登记待决事项并返回等待占位
    # （:func:`tool_awaiting_decision`），用户的决定作为消息交给模型。等待到用户回应为止——作答、跳过，
    # 或用户说了别的话（那句话就是回应，等待作废）；模型又问一次、发起它的 Run 被停下或失败也结束它
    # （规则见 ``decision_expiry``），也随撤回所在回合、Session 删除结束。
    # 这类工具一批里只能单独调用（见 ``tools_node``），呈现类别必须是 ``standalone``：
    # 等待时前端在调用的原位置画成卡片，收在工具组里会被折叠的组藏住（注册表守卫核对）。
    user_decision: bool = False
    # 决定携带的结构化载荷（见 :class:`DecisionPayload`）；``None`` 表示决定只有批准 / 驳回与附言。
    # 载荷渲染成给模型的正文，并进等待占位的展示数据。
    decision_model: type[DecisionPayload] | None = None
    # 要在用户能操作的界面里才用得了（交互终端、配置草稿）：只给侧栏会话。需要用户作答的工具
    # （``user_decision``）天然如此，不必重复声明，见 :attr:`requires_user_surface`。
    needs_user_surface: bool = False
    # 调用本身就是回复的一部分（回复下方的按钮、交付卡），不是过程：同一回复里它前面的正文与它之后的收尾
    # 一起算最终回复（规则只在 ``core/context/final_reply``）。模型常把答案和这类调用写在同一条回复里。
    part_of_reply: bool = False
    # 延迟加载：不进 Session 每次请求的基础 schema，模型在目录里看到名字、经 ``tool_search`` 加载后才可调用。
    # 给只在少数任务里用、定义又大的工具（浏览器一族等）；固化与加载见 ``app.models.session.session_tool_surface``。
    deferred: bool = False

    @property
    def requires_user_surface(self) -> bool:
        """是否只能给用户能操作的 Session（``models.session.session_kind.SessionKind.user_facing``）。"""
        return self.needs_user_surface or self.user_decision

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
            "parameters": self.parameters_schema or strict_parameters_schema(self.args_model),
        }
