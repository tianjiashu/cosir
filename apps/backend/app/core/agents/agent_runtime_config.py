"""Agent 运行时配置：一次 Run 实际执行所用的 Agent，是 Agent 定义与本次模型选择的最终解析。

两个构造入口分工明确：:meth:`AgentRuntimeConfig.for_run` 从 Run 的持久化事实（含其 Session 与工作区）
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
from app.models import RunRecord
from app.models.run_options import ReasoningEffort
from app.models.model_config_record import ModelConfigRecord
from app.service.depends import get_model_config_service, get_session_service, get_workspace_service


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """一次 Run 独占、不可变的 Agent 运行时配置。

    只在 Run 装配时经 :meth:`resolve` 构造一次，此后不再回看 ``AgentProfile``、``ModelSettings``
    或模型配置 ID：身份与执行资源取值即定，``model`` 是已解析完整的模型（连接、能力与偏好）。
    运行期消费方（模型构建、系统提示词、步数与输出契约）只读这里的字段。

    不含工具面：Session 能用哪些工具是 Session 创建时固化的事实（``SessionRuntimeSpace.session_tool_names``），
    定义里的 ``allowed_tools`` 只是固化时的输入，配置之后再改也不影响已有 Session。
    """

    agent_id: str
    agent_type: AgentProfileType
    role: str
    system_prompt: str
    max_steps: int
    structured_output: StructuredOutputSpec | None
    model: ResolvedModel

    @classmethod
    def for_run(cls, run: RunRecord) -> AgentRuntimeConfig:
        """合成本次 Run 独占的配置；注册表单例不被修改。

        Session 与工作区由 Run 反查：调用方手里只需要 Run 这一条事实（驱动与委派走同一条路径，不必各自
        先读一遍 Session / workspace）。定义只由持久化事实推导：按 Run 的 Agent 身份从注册表解析（主
        Agent 用系统作用域，其余用 Run 所属工作区）。连接与能力字段只由本 Run 的 ``model_config_id``
        物化。

        异常:
            RuntimeError: 注册表解析不到该 Agent，或 Run 未记录模型配置。
            KeyError: Run 指向的 Session / 工作区已不存在，或 ``model_config_id`` 指向的模型已不在。
            ModelConfigFileError: 模型配置文件无效。
        """

        session = get_session_service().get_session(run.session_id)
        workspace = get_workspace_service().get_workspace(session.workspace_id)
        agent_id = run.agent_id or "main_agent"
        workspace_scope = (
            AgentProfileRegistry.SYSTEM_WORKSPACE
            if agent_id == "main_agent"
            else workspace.root_path
        )
        definition = get_agent_registry().resolve(workspace_scope, agent_id)
        if definition is None:
            raise RuntimeError(f"agent profile unavailable for run {run.id}")
        # Run 行来自持久化边界：建 Run 的唯一路径总会写入模型配置，缺失只可能是脏数据。
        if run.model_config_id is None:
            raise RuntimeError(f"run {run.id} has no model config")
        model_config = get_model_config_service().get_config(run.model_config_id)
        return cls.resolve(definition, model_config, reasoning_effort=run.options.reasoning_effort)

    @classmethod
    def resolve(
        cls,
        definition: AgentProfile,
        model_config: ModelConfigRecord,
        *,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> AgentRuntimeConfig:
        """由 Agent 定义（注册表单例，不被修改）与本 Run 的模型选择解析。"""

        return cls(
            agent_id=definition.agent_id,
            agent_type=definition.agent_type,
            role=definition.role,
            system_prompt=definition.system_prompt,
            max_steps=definition.max_steps,
            structured_output=definition.structured_output,
            model=resolve_model(
                model_config, definition.model_settings, reasoning_effort=reasoning_effort
            ),
        )
