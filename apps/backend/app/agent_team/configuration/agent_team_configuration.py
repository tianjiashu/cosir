"""Agent Team 的可写入配置与图校验契约。

本模块聚合 Team 的静态配置根对象 :class:`AgentTeamConfiguration`，只负责把 JSON
配置解析成严格的领域对象，并执行不依赖运行时资源的图校验。Agent profile 的存在性
由 :mod:`app.service.configuration.agent_team_configuration_service` 负责；工具物化、
节点执行和数据库状态迁移由 Agent Team 运行层负责。

节点和状态转移的定义见 :mod:`app.agent_team.team_node_definition` 与
:mod:`app.agent_team.team_transition_definition`。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from app.agent_team.configuration.graph_validation import (
    TeamGraphValidationError,
    validate_team_graph,
)
from app.agent_team.configuration.team_node_definition import (
    TeamNodeDefinition,
    TeamNodeIdentifier,
)
from app.agent_team.configuration.team_transition_definition import TeamTransitionDefinition

TeamScope = Literal["system", "workspace"]


class TeamConfigurationError(ValueError):
    """Team 配置不符合静态契约时抛出的领域异常。

    该异常只表示静态配置或图结构无效，不表示 profile、模型或工具运行时资源
    不可用；运行时资源解析失败由预览构建层负责转换和报告。
    """


class AgentTeamConfiguration(BaseModel):
    """可写入 system/workspace 配置目录的 Team 配置。

    配置没有 ``version`` 字段。作用域由保存目录决定，``scope`` 只用于配置候选和
    API 请求的明确表达，不参与节点流转。

    Attributes:
        team_id: Team 的稳定标识，用作配置文件名和运行时引用，必须只包含字母、数字、
            下划线或连字符。
        name: 面向用户展示的 Team 名称。
        description: Team 的用途说明，帮助主 Agent 和用户理解配置适用场景。
        max_runs: 整个 Team 允许的最大执行轮数（节点执行次数），超过则收敛为失败。
        start_node_id: 唯一入口节点标识，必须声明在 ``nodes`` 中。
        nodes: 按节点定义组成的有序节点列表，通过 ``node_id`` 被转移规则引用。
        transitions: 节点业务状态到下一个节点的转移规则列表；目标为字面量 ``END``
            时结束 Team。
        scope: 配置作用域；``system`` 表示系统配置，``workspace`` 表示当前工作区配置。

    该模型只负责配置解析和不依赖运行时资源的图校验，不读取文件、不解析 Agent
    profile，也不启动节点执行。校验失败会抛出 ``ValidationError`` 或
    ``TeamConfigurationError``，由配置 API 或预览工具转换为用户可读错误。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: StrictStr = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Team 的稳定标识，也是配置文件和运行记录使用的逻辑 ID。",
    )
    name: StrictStr = Field(
        min_length=1,
        max_length=256,
        description="面向用户展示的 Team 名称。",
    )
    description: StrictStr = Field(
        min_length=1,
        max_length=2_000,
        description="Team 的用途和适用场景说明。",
    )
    max_runs: int = Field(
        default=10,
        ge=10,
        description="整个 Team 允许的最大执行轮数（节点执行次数），至少为 10；超过该轮数时 Team 收敛为失败。",
    )
    start_node_id: TeamNodeIdentifier = Field(
        description="唯一入口节点标识，必须声明在 nodes 中，且不能为 END。",
    )
    nodes: list[TeamNodeDefinition] = Field(
        min_length=1,
        max_length=256,
        description="Team 的节点定义列表；节点之间通过转移规则串行衔接。",
    )
    transitions: list[TeamTransitionDefinition] = Field(
        max_length=2_000,
        description="根据节点业务状态选择唯一下一节点的串行转移规则。",
    )
    scope: TeamScope = Field(
        default="workspace",
        description="配置作用域：system 或 workspace。",
    )

    @model_validator(mode="after")
    def validate_graph(self) -> AgentTeamConfiguration:
        """调用共享校验器检查节点类型、转移关系和图可达性。

        返回:
            当前已校验的不可变配置对象，便于 Pydantic 的 ``after`` 校验链继续处理。

        异常:
            TeamConfigurationError: 共享图校验器发现配置不符合静态契约时抛出。
        """

        try:
            validate_team_graph(self.nodes, self.transitions, self.start_node_id)
        except TeamGraphValidationError as exc:
            raise TeamConfigurationError(str(exc)) from exc
        return self

    def node(self, node_id: str) -> TeamNodeDefinition:
        """按节点标识返回节点定义。

        参数:
            node_id: 要查找的节点标识。

        异常:
            KeyError: 配置中不存在该节点时抛出。
        """

        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def transitions_for(self, node_id: str, status: str) -> list[TeamTransitionDefinition]:
        """返回指定节点和业务状态的唯一状态转移候选。

        参数:
            node_id: 当前提交结果的节点标识。
            status: 当前节点提交的业务状态。

        返回:
            只包含指定节点和状态的转移定义；返回新列表，不修改配置中的转移顺序。
        """

        return [
            transition
            for transition in self.transitions
            if transition.from_node_id == node_id and transition.status == status
        ]


def configuration_document(configuration: AgentTeamConfiguration) -> dict[str, object]:
    """返回可安全写入 Team JSON 文件的普通字典。"""

    return configuration.model_dump(mode="json", exclude_none=True)
