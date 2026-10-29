"""Agent 运行时配置：一次 Run 实际执行所用的 Agent，是 Agent 定义与本次模型选择的最终解析。

两个构造入口分工明确：:meth:`AgentRuntimeConfig.for_run` 从 Run 的持久化事实（含其 Task 与工作区）
推导定义与模型选择（唯一装配方），:meth:`AgentRuntimeConfig.resolve` 只接受已解析好的定义与
模型配置。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.configuration import get_agent_registry
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.resolved_model import ResolvedModel, resolve_model
from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.models import ConversationRunRecord
from app.models.agent_team.state.agent_team_run_state import AgentTeamRunRuntime
from app.models.conversation_run_options import ReasoningEffort
from app.models.model_config_record import ModelConfigRecord
from app.core.tool.handler.agent_team.node_profile import NODE_PROFILE_SNAPSHOT_KEY, build_node_profile
from app.service.depends import get_model_config_service, get_task_service, get_workspace_service


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """一次 Run 独占、不可变的 Agent 运行时配置。

    只在 Run 装配时经 :meth:`resolve` 构造一次，此后不再回看 ``AgentProfile``、``ModelSettings``
    或模型配置 ID：身份与执行资源取值即定，``model`` 是已解析完整的模型（连接、能力与偏好）。
    运行期消费方（模型构建、系统提示词、步数与输出契约、Agent Team 准备）只读这里的字段。
    """

    agent_id: str
    agent_type: AgentProfileType
    role: str
    system_prompt: str
    allowed_tools: tuple[str, ...]
    max_steps: int
    structured_output: StructuredOutputSpec | None
    model: ResolvedModel

    @classmethod
    def for_run(cls, run: ConversationRunRecord) -> AgentRuntimeConfig:
        """合成本次 Run 独占的配置；注册表单例与 Team 冻结定义都不被修改。

        Task 与工作区由 Run 反查：调用方手里只需要 Run 这一条事实（驱动、委派、Team 审批走同一条
        路径，不必各自先读一遍 Task / workspace）。定义只由持久化事实推导：按 Run 的 Agent 身份从
        注册表解析（主 Agent 用系统作用域，其余用 Run 所属工作区）；Team 节点 Task 再叠加其
        ``extra`` 里冻结的节点快照。连接与能力字段只由本 Run 的 ``model_config_id`` 物化——
        ``api_key`` 不落盘。

        异常:
            RuntimeError: 注册表解析不到该 Agent，或 Run 未记录模型配置。
            KeyError: Run 指向的 Task / 工作区已不存在，或 ``model_config_id`` 指向的配置已不存在。
            ModelSettingsError: 模型配置不完整。
        """

        task = get_task_service().get_task(run.task_id)
        workspace = get_workspace_service().get_workspace(task.workspace_id)
        agent_id = run.agent_id or "main_agent"
        workspace_scope = (
            AgentProfileRegistry.SYSTEM_WORKSPACE
            if agent_id == "main_agent"
            else workspace.root_path
        )
        definition = get_agent_registry().resolve(workspace_scope, agent_id)
        if definition is None:
            raise RuntimeError(f"agent profile unavailable for run {run.id}")
        node_snapshot = (task.extra or {}).get(NODE_PROFILE_SNAPSHOT_KEY)
        if node_snapshot is not None:
            definition = build_node_profile(
                definition, AgentTeamRunRuntime.model_validate(node_snapshot)
            )
        # Run 行来自持久化边界：建 Run 的唯一路径总会写入模型配置，缺失只可能是脏数据。
        if run.model_config_id is None:
            raise RuntimeError(f"run {run.id} has no model config")
        model_config = get_model_config_service().get_config(run.model_config_id)
        return cls.resolve(
            definition,
            model_config,
            reasoning_effort=run.options.reasoning_effort,
        )

    @classmethod
    def resolve(
        cls,
        definition: AgentProfile,
        model_config: ModelConfigRecord,
        *,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> AgentRuntimeConfig:
        """由 Agent 定义（注册表单例或 Team 节点冻结定义，不被修改）与本 Run 的模型选择解析。

        异常:
            ModelSettingsError: 模型配置不完整（见 :func:`resolve_model`）。
        """

        return cls(
            agent_id=definition.agent_id,
            agent_type=definition.agent_type,
            role=definition.role,
            system_prompt=definition.system_prompt,
            allowed_tools=tuple(definition.allowed_tools),
            max_steps=definition.max_steps,
            structured_output=definition.structured_output,
            model=resolve_model(
                model_config, definition.model_settings, reasoning_effort=reasoning_effort
            ),
        )
