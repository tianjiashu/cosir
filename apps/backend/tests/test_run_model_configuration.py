from datetime import UTC, datetime

import pytest

from app.core.agents.agent_profile import AgentProfile
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.agents.model_settings import ModelSettings, ModelSettingsError
from app.core.agents.resolved_model import ResolvedModel
from app.core.llm_provider import model_factory
from app.core.workflow.message_queue.session_message import RunLaunch
from app.models.enums.reasoning_effort import ReasoningEffort
from app.models.model_config_record import ModelConfigRecord
from app.models.run_record import RunRecord
from tests.model_config_fixtures import model_config_record


def _run(*, reasoning_effort: ReasoningEffort | None) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        id=1,
        session_id=2,
        status="running",
        created_at=now,
        updated_at=now,
        model_config_id="test/m7",
        reasoning_effort=reasoning_effort,
    )


def _profile() -> AgentProfile:
    """Agent 定义只带用户偏好；连接与能力字段来自本 Run 选中的模型配置。"""
    return AgentProfile(
        agent_id="main_agent",
        role="主 Agent",
        system_prompt="system",
        model_settings=ModelSettings(temperature=0.2, reasoning_effort="high"),
    )


def _model_config() -> ModelConfigRecord:
    return model_config_record(
        name="run", base_url="https://example.test", api_key="secret",
        model_name="profile-model",
        context_window_k=64,
        supports_thinking=False,
        supports_reasoning_effort=True,
        supports_image=False,
    )


def test_run_reasoning_effort_overrides_agent_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _build_chat_model(model_settings: ResolvedModel, *, tools):
        captured.update(
            {
                "model_name": model_settings.model_name,
                "reasoning_effort": model_settings.reasoning_effort,
                "temperature": model_settings.temperature,
                "tools": tools,
            }
        )
        return object()

    monkeypatch.setattr(
        model_factory,
        "build_chat_model",
        _build_chat_model,
    )

    agent = AgentRuntimeConfig.resolve(_profile(), _model_config(), reasoning_effort="low")
    resolved = model_factory.resolve_chat_model(
        model=agent.model,
        tools=[{"name": "read_file"}],
    )

    assert captured == {
        "model_name": "profile-model",
        "reasoning_effort": "low",
        "temperature": 0.2,
        "tools": [{"name": "read_file"}],
    }
    assert resolved.model_name == "profile-model"
    assert resolved.context_window_k == 64
    assert resolved.supports_thinking is False


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_profile_reasoning_effort_survives_configuration_and_run_round_trip(effort):
    settings = ModelSettings.from_json({"reasoning_effort": effort, "temperature": 0.2})
    launch = RunLaunch(model_config_id="test/m7", reasoning_effort=settings.reasoning_effort)
    assert RunLaunch.model_validate(launch.model_dump()).reasoning_effort == effort
    assert settings.to_dict() == {"reasoning_effort": effort, "temperature": 0.2}


@pytest.mark.parametrize("effort", ["medium", "", "HIGH", 1, True, None])
def test_invalid_profile_reasoning_effort_fails_at_configuration_boundary(effort):
    with pytest.raises(ModelSettingsError):
        ModelSettings.from_json({"reasoning_effort": effort})
