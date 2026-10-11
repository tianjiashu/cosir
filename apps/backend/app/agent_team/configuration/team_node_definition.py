"""Team 节点定义及其字段校验。

本模块只描述单个节点的静态配置，不解析 Agent profile、不物化工具，也不负责节点
运行时状态和下一个节点的选择。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

TeamNodeIdentifier = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]
TeamStatusName = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]
TeamStatusColor = Annotated[
    StrictStr,
    StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$"),
]


class TeamStatusDefinition(BaseModel):
    """描述一个业务状态及其供画布和状态标签使用的颜色。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: TeamStatusName = Field(description="节点返回的业务状态名称。")
    color: TeamStatusColor = Field(description="状态颜色，使用 #RRGGBB 十六进制格式。")

    @field_validator("color", mode="before")
    @classmethod
    def normalize_color(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class TeamNodeDefinition(BaseModel):
    """描述一个串行 Team 节点及其业务状态集合。

    Attributes:
        node_id: 节点在 Team 配置中的稳定唯一标识，也是转移规则引用节点的键。
        name: 面向用户展示的节点名称。
        agent_id: 节点执行时使用的 child Agent profile 标识；profile 是否存在由运行时
            注册表和预览构建层校验。
        statuses: 节点允许提交的业务状态及其展示颜色；状态名称不是 ConversationRun
            的技术生命周期状态。

    节点定义是不可变配置对象。它不会创建数据库记录或启动执行；字段非法时由
    Pydantic 抛出 ``ValidationError``。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: TeamNodeIdentifier = Field(
        description="节点的稳定唯一标识，也是转移规则引用节点的键。",
    )
    name: StrictStr = Field(
        min_length=1,
        max_length=256,
        description="面向用户展示的节点名称。",
    )
    agent_id: TeamNodeIdentifier = Field(
        description="节点使用的 child Agent profile 标识。",
    )
    statuses: list[TeamStatusDefinition] = Field(
        min_length=1,
        max_length=32,
        description="节点允许提交的小写 snake_case 业务状态及其十六进制颜色。",
    )

    @model_validator(mode="after")
    def validate_statuses(self) -> TeamNodeDefinition:
        """拒绝空状态名和重复状态，保持转移匹配确定。

        返回:
            当前已校验的节点定义。

        异常:
            ValueError: 状态名称为空白或重复时抛出，由 Pydantic 包装为校验错误。
        """

        if len({status.name for status in self.statuses}) != len(self.statuses):
            raise ValueError("node statuses must not contain duplicates")
        return self
