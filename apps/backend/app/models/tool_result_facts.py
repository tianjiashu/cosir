"""工具结果行的持久化事实：工具交给运行时、而不是给模型或界面卡片的结构化结果。

模型只读观察的 ``content``，卡片只读 ``display_data``；有些工具结果还要成为后端事实，被后续逻辑读取——交付记录
（产出清单与交付卡）、加载的工具（下一次请求的工具集与准入）。这些不能从 ``content`` 反解：正文要过输出
预算、可能被截断，也不带执行要用的定位信息，后端规则也不解析正文。

通路只有一条：工具经 ``tool_success(facts=...)`` 交出，``ToolObservation.facts`` → 结果行
``ToolMessageMetadata.facts`` 原样持久化，随上下文继承（分叉）、移出（撤回）、重建（恢复、重启）。新的一类事实
在这里加一个字段，构造、投递与持久化都不用改。每个字段只属于一个工具。
"""

from pydantic import BaseModel, ConfigDict

from app.models.deliverable import Deliverable
from app.models.external_tool import ExternalToolDefinition


class ToolResultFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # 交付记录（只有 ``present_deliverables``）：卡片与产出清单的事实源，见 ``app.models.deliverable``。
    deliverables: tuple[Deliverable, ...] | None = None
    # ``tool_search`` 加载的工具：两项合起来是「已加载」的唯一事实（见 ``RequestTools``）。外部工具存定义
    # 快照（见 ``app.models.external_tool``）；内置工具只存名字，定义取 Session 工具面固化的那一份。
    loaded_external_tools: tuple[ExternalToolDefinition, ...] | None = None
    loaded_builtin_tools: tuple[str, ...] | None = None
