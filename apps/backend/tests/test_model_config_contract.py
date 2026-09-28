"""模型连接配置的新契约测试。"""

from datetime import UTC, datetime

import pytest

from app.api.schemas.request.ModelConfigCreateRequest import ModelConfigCreateRequest
from app.api.schemas.response.ModelConfigResponse import ModelConfigResponse
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
    """未保存测试必须把表单中的地址、Key 和模型名送入真实探测入口。"""

    calls: list[dict[str, str]] = []

    async def fake_ping(**payload: str) -> None:
        calls.append(payload)

    monkeypatch.setattr(ModelConfigService, "_acompletion_ping", staticmethod(fake_ping))
    service = object.__new__(ModelConfigService)

    result = await service.test_draft(
        base_url="https://example.com/v1",
        api_key="secret",
        model_name="demo-model",
    )

    assert result.success is True
    assert calls == [
        {
            "base_url": "https://example.com/v1",
            "api_key": "secret",
            "model": "demo-model",
        }
    ]
