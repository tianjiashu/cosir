"""Agent 运行时配置：一次 Run 实际执行所用的 Agent，是 Agent 定义与本次模型选择的最终解析。"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.resolved_model import ResolvedModel, resolve_model
from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.models.conversation_run_options import ReasoningEffort
from app.models.model_config_record import ModelConfigRecord


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
