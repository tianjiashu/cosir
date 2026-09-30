"""模型连接配置的新契约测试。"""

from datetime import UTC, datetime

import pytest

from app.api.schemas.request.ModelConfigCreateRequest import ModelConfigCreateRequest
from app.api.schemas.request.ModelConfigTestRequest import ModelConfigTestRequest
from app.api.schemas.response.ModelConfigResponse import ModelConfigResponse
from app.core.agents.model_settings import ModelSettings
from app.core.llm_provider.model_probe import ModelProbeFailureKind, ModelProbeOutcome
from app.models.model_config_record import ModelConfigRecord
from app.service.model_config.model_config_service import ModelConfigService
from app.storage.model.model_config_model import ModelConfigModel


def test_model_config_requires_connection_fields_and_context_window() -> None:
    """配置必须包含名称、OpenAI 兼容地址、Key、模型名和 K 单位上下文窗口。"""

    config = ModelConfigCreateRequest(
        config_name="本地模型",
        base_url="https://example.com/v1/",
        api_key="secret",
        model_name="demo-model",
        context_window_k=128,
    )

    assert config.config_name == "本地模型"
    assert config.base_url == "https://example.com/v1"
    assert config.context_window_k == 128

    with pytest.raises(ValueError):
        ModelConfigCreateRequest(
            config_name="缺窗口",
            base_url="https://example.com/v1",
            api_key="secret",
            model_name="demo-model",
        )

    test_request = ModelConfigTestRequest(
        config_name="本地模型",
        base_url="https://example.com/v1/",
        api_key="secret",
        model_name="demo-model",
        context_window_k=128,
    )
    assert test_request.base_url == "https://example.com/v1"

    with pytest.raises(ValueError):
        ModelConfigTestRequest(
            config_name="缺 Key",
            base_url="https://example.com/v1",
            api_key=" ",
            model_name="demo-model",
            context_window_k=128,
        )


def test_model_config_model_has_no_provider_columns() -> None:
    """模型配置表只描述连接配置，不再声明 Provider 厂商字段。"""

    columns = set(ModelConfigModel.__table__.c.keys())

    assert {"config_name", "base_url", "api_key", "model_name", "context_window_k"} <= columns
    assert "name" not in columns
    assert "type" not in columns


def test_model_config_response_returns_api_key_for_local_edit_form() -> None:
    """本机编辑表单需要回显 API Key，响应应携带明文供前端以密码态展示。"""

    now = datetime.now(UTC)
    record = ModelConfigRecord(
        id=1,
        config_name="测试配置",
        base_url="https://example.com/v1",
        api_key="secret",
        model_name="demo-model",
        context_window_k=64,
        created_at=now,
        updated_at=now,
    )

    response = ModelConfigResponse.from_record(record)

    assert response.config_id == 1
    assert response.context_window_k == 64
    assert response.api_key_configured is True
    assert response.api_key == "secret"


@pytest.mark.asyncio
async def test_draft_connection_test_uses_the_submitted_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未保存测试必须把完整表单物化后送入真实探活入口。"""

    captured: list[ModelSettings] = []

    async def fake_probe(model_settings: ModelSettings, **kwargs: object) -> ModelProbeOutcome:
        captured.append(model_settings)
        return ModelProbeOutcome(ok=True, elapsed_ms=12)

    monkeypatch.setattr(
        "app.service.model_config.model_config_service.probe_chat_model",
        fake_probe,
    )
    service = object.__new__(ModelConfigService)

    result = await service.test_draft(
        config_name="本地模型",
        base_url="https://example.com/v1",
        api_key="secret",
        model_name="demo-model",
        context_window_k=128,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_image=True,
    )

    assert result.success is True
    assert result.elapsed_ms == 12
    assert len(captured) == 1
    settings = captured[0]
    assert settings.base_url == "https://example.com/v1"
    assert settings.api_key == "secret"
    assert settings.model_name == "demo-model"
    assert settings.context_window_k == 128
    assert settings.supports_thinking is True
    assert settings.supports_reasoning_effort is True
    assert settings.supports_image is True


@pytest.mark.asyncio
async def test_connection_failure_exposes_stable_code_and_controlled_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探活失败必须透出稳定 error_code 与受控文案，且不得泄漏探活细节。"""

    async def fake_probe(model_settings: ModelSettings, **kwargs: object) -> ModelProbeOutcome:
        return ModelProbeOutcome(
            ok=False,
            elapsed_ms=608,
            failure_kind=ModelProbeFailureKind.INVALID_RESPONSE,
            detail="<!doctype html>",
        )

    monkeypatch.setattr(
        "app.service.model_config.model_config_service.probe_chat_model",
        fake_probe,
    )
    service = object.__new__(ModelConfigService)
    result = await service.test_draft(
        config_name="packyapi",
        base_url="https://www.packyapi.ai",
        api_key="secret",
        model_name="qwen3.8-flash",
        context_window_k=1000,
        supports_thinking=False,
        supports_reasoning_effort=False,
        supports_image=False,
    )

    assert result.success is False
    assert result.error_code == ModelProbeFailureKind.INVALID_RESPONSE.value
    assert result.elapsed_ms == 608
    assert result.error_message
    assert "<!doctype" not in (result.error_message or "")


def test_service_has_no_second_model_call_implementation() -> None:
    """连接测试不得再自行实现一套 HTTP 调用。

    历史事故：手写非流式请求 + 只看 HTTP 状态码，把返回 HTML 首页的端点判为成功，而运行期
    因流里没有 chunk 直接失败。此断言防止第二套实现被重新引入。
    """

    assert not hasattr(ModelConfigService, "_acompletion_ping")
