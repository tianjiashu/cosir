"""用户对待决调用的结构化决定；供工具、工作流和 Run 持久进度共用。

工具层与工作流层共用的纯值对象：``ToolCall.user_decision`` 与
``ToolExecutionContext.user_decision`` 据此把「本调用已被用户批准」交给 handler，
handler 据此走真正的执行分支，而不是再次产出待决请求。

本模块只承载决定的语义（谁被问、用户选了什么、附带了什么结构化输入）。请求侧的形状
（``prompt`` / ``decisions`` / ``draft``）属于 ``UserInputRequest``，本模块不重复承载；也不承载
wire 字段名——wire 与领域参数的映射由 Assistant Transport 边界负责。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserDecisionKind(str, Enum):
    """用户可作出的决定种类（框架级闭集）。

    路由目标由工作流的固定映射决定，提问工具不能改写：

    - ``APPROVE``：批准该调用。``tools`` 节点以「已确认」上下文重新执行该调用。
    - ``REJECT``：驳回该调用。不执行，用户意见内联进该调用的观察交给模型重新提案。
    - ``ABORT``：放弃该调用。不执行，观察按取消收口，模型按「被用户取消」继续。
    - ``FORCE_REJECT``：强行驳回**当前唯一**的待决请求，不核对 ``request_id``，也不要求请求
      声明过该种类。它由系统合成（取消一个停在 ``wait_user`` 断点上的 Run），不是用户在卡片上
      做的选择：消费方在 ``wait_user`` 内把它归一为对该请求的普通 ``REJECT``，因此校验、观察
      改写与领域收尾与真实驳回同构。客户端不得提交它。
    """

    APPROVE = "approve"
    REJECT = "reject"
    ABORT = "abort"
    FORCE_REJECT = "force_reject"


# 决定是否真正「执行调用」的判据收口在此，避免各调用点各自比较枚举字面量。
EXECUTING_DECISION_KINDS: frozenset[UserDecisionKind] = frozenset({UserDecisionKind.APPROVE})


class UserDecision(BaseModel):
    """一次等待请求的用户决定。

    Attributes:
        request_id: 决定对应的等待请求标识，取自提问工具提交的
            ``ToolObservation.user_input_request.request_id``（工作流控制流事实，不放在
            ``display_data`` 里）。``FORCE_REJECT`` 不定位请求，允许为空串。
        kind: 用户作出的决定种类。
        data: 用户编辑后的结构化输入；形状由提问工具自己定义并在消费点校验，
            框架只负责透传，不解释其字段含义。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = ""
    kind: UserDecisionKind
    data: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "EXECUTING_DECISION_KINDS",
    "UserDecision",
    "UserDecisionKind",
]
