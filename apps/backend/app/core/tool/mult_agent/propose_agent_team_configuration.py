"""ProposeAgentTeamConfigurationTool：生成不落盘的 Team 配置候选。"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from app.config.logging.logger import log
from app.core.tool.mult_agent.agent_team.team_tool_error import TeamToolError
from app.core.tool.mult_agent.args.propose_agent_team_configuration_args import (
    ProposeAgentTeamConfigurationArgs,
)
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_names import TOOL_PROPOSE_AGENT_TEAM_CONFIGURATION
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_CONFIGURATION


class ProposeAgentTeamConfigurationTool(HandlerBase):
    """生成不落盘的 Team 配置候选。"""

    name = TOOL_PROPOSE_AGENT_TEAM_CONFIGURATION
    description = (
        "Create an Agent Team configuration draft for user review. Declare exactly one entry "
        "node via start_node_id, and finish the Team by routing transitions to the literal "
        "target END. Every node must be reachable from the entry and able to reach END. "
        "Cycles are allowed; max_runs bounds repeated execution. This tool only drafts config; "
        "it must not start a Team run or save the config."
    )
    args_model = ProposeAgentTeamConfigurationArgs
    timeout_seconds: ClassVar[float] = 20.0
    group = TOOL_GROUP_CONFIGURATION

    def execute(
        self,
        execution_context: ToolExecutionContext | None = None,
        **kwargs: object,
    ) -> ToolObservation:
        """校验配置候选并返回前端可审阅的展示数据。"""

        try:
            from app.core.tool.mult_agent.agent_team.agent_team_preparation_service import (
                resolve_node_profile,
            )

            # 提案只生成待审阅的工具输入预览，不提前构造持久化配置对象。用户确认
            # 保存时，保存 API 会用用户实际选择的 scope 重新校验领域配置。
            proposal: ProposeAgentTeamConfigurationArgs = self.args_model.model_validate(kwargs)
            for node in proposal.nodes:
                resolve_node_profile(
                    proposal,
                    str(execution_context.workspace_root),
                    node.node_id,
                )
            return tool_success(
                tool_name=self.name,
                content="Agent Team configuration draft is ready for user review.",
                display_data=self._configuration_display_data(proposal, scope="workspace"),
            )
        except ValueError as exc:
            log.warning(
                "agent_team_configuration_proposal_failed",
                extra={"msg": "Agent Team 配置提案校验失败", "error_type": type(exc).__name__},
            )
            return tool_error(
                tool_name=self.name,
                content=str(exc),
            )
        except TeamToolError as exc:
            log.warning(
                "agent_team_configuration_proposal_failed",
                extra={"msg": "Agent Team 配置引用校验失败", "error_type": type(exc).__name__},
            )
            return tool_error(
                tool_name=self.name,
                content=str(exc),
            )
        except Exception as exc:
            log.warning(
                "agent_team_configuration_proposal_failed",
                extra={"msg": "Agent Team 配置提案校验失败", "error_type": type(exc).__name__},
            )
            return tool_error(
                tool_name=self.name,
                content="The Team configuration is invalid.",
            )

    @staticmethod
    def _configuration_display_data(
        configuration: ProposeAgentTeamConfigurationArgs,
        *,
        scope: Literal["system", "workspace"],
    ) -> dict[str, Any]:
        """构造不包含 system prompt 和密钥的配置候选展示。

        参数:
            configuration: 已通过对应入口校验的提案模型或持久化配置模型。
            scope: 当前展示使用的目标作用域；提案阶段固定为 ``workspace``，不写入配置事实。

        返回:
            面向前端 Transport 的结构化展示数据。

        异常:
            KeyError 或 ValueError: 输入对象缺少预览所需字段时抛出，由工具层转换为工具错误。
        """

        document = configuration.model_dump(mode="json")
        document["scope"] = scope

        return {
            "kind": "agent-team-configuration-draft",
            "status": "draft",
            "team_id": configuration.team_id,
            "name": configuration.name,
            "description": configuration.description,
            "scope": scope,
            "start_node": configuration.start_node_id,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "name": node.name,
                    "agent_id": node.agent_id,
                    "node_type": (
                        "start" if node.node_id == configuration.start_node_id else "middle"
                    ),
                    "statuses": [status.model_dump(mode="json") for status in node.statuses],
                }
                for node in configuration.nodes
            ],
            "edges": [
                transition.model_dump(mode="json") for transition in configuration.transitions
            ],
            "configuration": document,
        }

    def to_definition(self) -> ToolDefinition:
        """构造配置候选工具定义。"""

        return ToolDefinition(
            name=self.name,
            group=self.group,
            needs_user_surface=True,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            execution_mode="thread",
        )


def build_propose_agent_team_configuration_definition() -> ToolDefinition:
    """构造 Team 配置候选工具定义。"""

    return ProposeAgentTeamConfigurationTool().to_definition()
