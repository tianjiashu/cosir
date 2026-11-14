"""用户决定携带的结构化载荷：``reply`` 类 HITL 工具声明的回答形状。"""

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class DecisionPayload(BaseModel, ABC):
    """用户对一次 HITL 调用作出决定时提交的结构化回答，与 ``args_model`` 对称。

    工具在 ``ToolDefinition.decision_model`` 上声明子类；决定入口（``ToolDecisionService``）是
    唯一校验点：批准时按子类校验前端输入，并把原调用的参数作为 pydantic 校验上下文
    （``info.context["args"]``）交进来——所选项是否属于这次提问，只有原调用知道。校验后的载荷
    随 ``DecisionBody.data`` 入队，并入时并进等待占位的展示数据；给模型的正文由 :meth:`reply`
    在入队时渲染一次（回执格式是工具的事），之后不再读注册表。

    为什么是基类而不是具体类型：``ToolDefinition`` 在 ``core/tool/schemas``，不能反向依赖某个
    工具的模型；注册表按同一契约装配各工具的载荷。
    """

    model_config = ConfigDict(extra="forbid")

    # 驳回（不带载荷）时给模型的正文。
    DECLINED_REPLY: ClassVar[str]

    @abstractmethod
    def reply(self) -> str:
        """批准时给模型的正文（不含 ``<user_decision>`` 包装）。"""
