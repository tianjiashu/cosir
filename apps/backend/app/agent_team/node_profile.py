"""Team 节点 Agent 定义：注册表身份与确认时冻结的节点快照合成。

节点 Task 创建时把节点快照写进 ``task.extra[NODE_PROFILE_SNAPSHOT_KEY]``；每次驱动节点 Run 时由
``run_driver`` 读回并在这里合成定义，不经调度入口在内存中传递 profile——交接、重启后的恢复都只
依赖持久化事实。
"""

from dataclasses import replace

from app.agent_team.state.agent_team_run_state import AgentTeamRunRuntime
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.model_settings import ModelSettings
from app.core.agents.structured_output_spec import StructuredOutputSpec

NODE_PROFILE_SNAPSHOT_KEY = "agent_team_profile_snapshot"


def build_node_profile(
    agent_profile: AgentProfile,
    node_runtime: AgentTeamRunRuntime,
) -> AgentProfile:
    """把「注册表节点身份」与「确认时冻结的节点快照」合成节点的基础 profile。

    字段来源刻意分路，不要归并：身份类（``agent_id`` / ``agent_type`` / ``max_steps``）来自
    注册表；执行资源（提示词、工具名、结构化输出契约、用户模型偏好）来自节点快照，保证节点
    行为与用户确认时预览的一致。连接与能力字段由运行时按节点 Run 的 ``model_config_id`` 物化
    （``api_key`` 不落盘），快照里残留的连接字段经 ``ModelSettings.from_dict`` 忽略。

    异常:
        pydantic.ValidationError: 快照的 ``structured_output`` 不是合法的输出契约。
    """
    return replace(
        agent_profile,
        role=node_runtime.role or agent_profile.role,
        system_prompt=node_runtime.system_prompt,
        allowed_tools=list(node_runtime.allowed_tools),
        model_settings=ModelSettings.from_dict(node_runtime.model_settings),
        structured_output=(
            StructuredOutputSpec.model_validate(node_runtime.structured_output)
            if node_runtime.structured_output
            else None
        ),
    )
