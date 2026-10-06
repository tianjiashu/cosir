"""按需生成子 Agent 配置草稿的临时工具。"""

from typing import ClassVar

from app.core.tools.display.agent_configuration_display import (
    build_agent_configuration_draft_display_data,
)
from app.core.tools.schemas import (
    TOOL_PROPOSE_AGENT_CONFIGURATION,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_grouping import TOOL_GROUP_CONFIGURATION
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import ProposeAgentConfigurationArgs


class ProposeAgentConfigurationTool(HandlerBase):
    """返回一份未落盘的子 Agent 配置草稿。

    工具只做参数归一化和展示数据投影，不访问文件、数据库或配置 service；保存动作由用户在
    Workbench 中确认后调用既有 Agent 配置 API 完成。
    """

    name = TOOL_PROPOSE_AGENT_CONFIGURATION
    description = (
        "Create a draft child-agent configuration for the user to review. Only generate "
        "agent_id, role, description, and system_prompt. Do not save files or database "
        "records; the user will configure scope, tools, steps, and model in the editor."
    )
    permission: ClassVar[str] = "agent_configuration_proposal"
    args_model = ProposeAgentConfigurationArgs
    timeout_seconds: ClassVar[float] = 10.0
    risk_level: ClassVar[str] = "low"
    group = TOOL_GROUP_CONFIGURATION

    def execute(
        self,
        agent_id: str,
        role: str,
        description: str,
        system_prompt: str,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """生成模型可继续消费且前端可编辑的临时配置草稿。

        ``execution_context`` 用于定位当前 workspace 以校验 ``agent_id`` 唯一性（在
        当前 workspace 与 system 作用域内不可重复）。返回结果不产生持久化副作用；参数异常
        由 ToolHandlerRunner 在调用前归一化。
        """
        candidate_agent_id = agent_id.strip()
        if execution_context is not None:
            from app.config.configuration import get_agent_registry

            existing_agent_ids = get_agent_registry().list_agent_ids(
                execution_context.workspace_root
            )
            if candidate_agent_id in existing_agent_ids:
                return tool_error(
                    tool_name=self.name,
                    permission=self.permission,
                    error=(
                        f"agent_id '{candidate_agent_id}' is already in use in this "
                        "workspace or the system scope."
                    ),
                    reason="choose a different agent_id that is not already taken.",
                    retryable=True,
                    tool_call_id=execution_context.tool_call_id,
                )

        payload = {
            "agent_id": candidate_agent_id,
            "role": role.strip(),
            "description": description.strip(),
            "system_prompt": system_prompt.strip(),
        }
        display_data = build_agent_configuration_draft_display_data(**payload)
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content="A child-agent configuration draft is ready for user review in the editor.",
            display_data=display_data,
        )

    def to_definition(self) -> ToolDefinition:
        """构建临时配置提案工具的注册定义。"""

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="生成子 Agent 配置草稿",
                icon="bot",
                surface="standalone",
                expandable=False,
                expand_layout="details",
                show_result=False,
            ),
        )


def build_propose_agent_configuration_definition() -> ToolDefinition:
    """构建并返回配置提案工具定义。"""

    return ProposeAgentConfigurationTool().to_definition()
