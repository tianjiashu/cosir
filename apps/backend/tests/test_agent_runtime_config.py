"""``AgentRuntimeConfig.resolve`` 的解析契约：Agent 定义 + 本 Run 选中的模型配置 → 扁平运行时配置。

只验证解析规则本身：身份与执行资源取自定义、连接与能力字段只来自模型配置、定义提供偏好、Run
推理强度最终覆盖、不完整配置在装配期失败、结果不持有任何来源对象且不可变。
"""

from dataclasses import FrozenInstanceError, fields

import pytest

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.agents.model_settings import ModelSettings, ModelSettingsError
from app.core.agents.resolved_model import ResolvedModel
from app.models.model_config_record import ModelConfigRecord
from tests.model_config_fixtures import model_config_record


def _definition(model_settings: ModelSettings | None = None) -> AgentProfile:
    return AgentProfile(
        agent_id="code-developer",
        role="developer",
        allowed_tools=["read_file", "write_file"],
        agent_type=AgentProfileType.CHILD,
        system_prompt="Test child system prompt.",
        model_settings=model_settings or ModelSettings(),
        max_steps=7,
    )


def _model_config() -> ModelConfigRecord:
    return model_config_record(
        connection_id="run",
        base_url="https://run.test",
        api_key="run-key",
        model_name="run-model",
        context_window_k=64,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_image=True,
    )


def test_resolve_flattens_definition_and_model_config() -> None:
    definition = _definition(ModelSettings(temperature=0.2, top_p=0.5))

    config = AgentRuntimeConfig.resolve(definition, _model_config())

    assert (config.agent_id, config.role, config.agent_type) == (
        "code-developer",
        "developer",
        AgentProfileType.CHILD,
    )
    assert config.max_steps == 7 and config.structured_output is None
    model = config.model
    assert model.model_config_id == "run/run-model"
    assert (model.base_url, model.api_key, model.model_name) == (
        "https://run.test",
        "run-key",
        "run-model",
    )
    assert model.context_window_k == 64 and model.supports_image is True
    assert (model.temperature, model.top_p) == (0.2, 0.5)


def test_runtime_config_holds_no_source_objects() -> None:
    """解析结果只含扁平字段：不持有 AgentProfile、ModelSettings，也没有顶层模型配置 ID。"""
    config = AgentRuntimeConfig.resolve(_definition(), _model_config())

    values = [getattr(config, item.name) for item in fields(config)]
    assert not any(isinstance(value, AgentProfile | ModelSettings) for value in values)
    assert not hasattr(config, "model_config_id")
    assert isinstance(config.model, ResolvedModel)


def test_api_key_is_not_in_repr() -> None:
    config = AgentRuntimeConfig.resolve(_definition(), _model_config())

    assert "run-key" not in repr(config)


def test_run_reasoning_effort_overrides_definition_preference() -> None:
    definition = _definition(ModelSettings(reasoning_effort="high"))

    config = AgentRuntimeConfig.resolve(definition, _model_config(), reasoning_effort="low")

    assert config.model.reasoning_effort == "low"


def test_definition_preference_is_kept_without_run_override() -> None:
    definition = _definition(ModelSettings(reasoning_effort="high"))

    config = AgentRuntimeConfig.resolve(definition, _model_config())

    assert config.model.reasoning_effort == "high"


def test_runtime_config_is_immutable() -> None:
    config = AgentRuntimeConfig.resolve(_definition(), _model_config())

    with pytest.raises(FrozenInstanceError):
        config.max_steps = 4  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        config.model.model_name = "other"  # type: ignore[misc]


def test_model_settings_rejects_connection_fields() -> None:
    """偏好对象不再接受连接字段：Agent JSON 里写入它们按未知字段拒绝。"""
    with pytest.raises(ModelSettingsError):
        ModelSettings.from_json({"base_url": "https://evil.test"})
    with pytest.raises(TypeError):
        ModelSettings(api_key="x")  # type: ignore[call-arg]
