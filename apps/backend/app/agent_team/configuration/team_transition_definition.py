"""Team 状态转移定义。

描述某节点某个业务状态对应的下一个节点。Team 是否结束由目标节点的类型决定，终点
节点不需要配置转移。Team 不支持节点并行，因此每条转移只有一个目标节点。
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr, StringConstraints

TeamTransitionIdentifier = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]
TeamTransitionStatus = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]


class TeamTransitionDefinition(BaseModel):
    """描述某节点某个业务状态对应的唯一下一节点。

    Attributes:
        from_node_id: 发起转移的源节点标识。
        status: 源节点提交后触发候选转移的业务状态。
        target_node_id: 下一个串行节点标识；Team 到达 ``end`` 类型节点后终止。

    转移定义是不可变配置对象，不负责修改 TeamRun 或启动节点。字段非法时由 Pydantic
    抛出 ``ValidationError``。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_node_id: TeamTransitionIdentifier = Field(
        description="发起转移的源节点标识。",
    )
    status: TeamTransitionStatus = Field(
        description="源节点提交后触发候选转移的小写 snake_case 业务状态。",
    )
    target_node_id: TeamTransitionIdentifier = Field(
        description="唯一下一节点标识；目标节点类型为 end 时，执行该节点后结束 Team。",
    )
