"""模型配置契约：文件格式、请求边界与连接测试。"""

import json

import pytest

from app.api.schemas.request.ModelConfigsAddRequest import ModelConfigsAddRequest
from app.api.schemas.request.ModelConfigTestRequest import ModelConfigTestRequest
from app.api.schemas.request.ModelConnectionCreateRequest import ModelConnectionCreateRequest
from app.api.schemas.response.ModelConnectionResponse import ModelConnectionResponse
from app.core.agents.resolved_model import ResolvedModel
from app.core.llm_provider.model_probe import ModelProbeFailureKind, ModelProbeOutcome
from app.models.model_config_file import ModelConfigFile, ModelConnectionEntry, ModelEntry
from app.service.model_config.model_config_service import ModelConfigService


def _connection(**overrides: object) -> dict[str, object]:
    return {"id": "packy", "name": "Packy", "base_url": "u", "api_key": "k", **overrides}


def test_file_accepts_minimal_hand_written_models_and_ids_survive_slashes() -> None:
    """手写时能力开关可省略；模型名带 ``/`` 时 id 仍按第一个 ``/`` 拆分。"""

    document = ModelConfigFile.model_validate_json(
        '{"connections": [{"id": "packy", "name": "Packy", "base_url": "u", "api_key": "k",'
        ' "models": [{"model_name": "deepseek-ai/DeepSeek-V3", "context_window_k": 64}]}]}'
    )

    connection, model = document.find("packy/deepseek-ai/DeepSeek-V3")
    assert (connection.name, model.supports_image) == ("Packy", False)
    with pytest.raises(KeyError):
        document.find("packy/missing")
    with pytest.raises(KeyError):
        document.find("nobody/deepseek-ai/DeepSeek-V3")


@pytest.mark.parametrize(
    ("connections", "message"),
    [
        ([_connection(extra=1)], "extra"),
        ([_connection(id="Packy")], "pattern"),
        ([_connection(id="a/b")], "pattern"),
        ([_connection(models=[{"model_name": "m", "context_window_k": 0}])], "greater than 0"),
        (
            [_connection(models=[{"model_name": "m", "context_window_k": 8}] * 2)],
            "重名模型",
        ),
        ([_connection(), _connection(name="Other")], "id 不能重复"),
        ([_connection(), _connection(id="other")], "name 不能重复"),
    ],
)
def test_file_rejects_what_would_break_lookups(connections: list, message: str) -> None:
    """多余字段、非法连接 id、非正上下文窗口与各种重名都在读取边界拒绝。"""

    with pytest.raises(ValueError, match=message):
        # 文件只经 JSON 读取：严格模式下按 JSON 语义校验（数组即元组，不做类型转换）。
        ModelConfigFile.model_validate_json(json.dumps({"connections": connections}))


def test_new_connection_id_is_a_free_slug() -> None:
    document = ModelConfigFile(connections=(ModelConnectionEntry(**_connection()),))

    assert document.new_connection_id("Packy Code!") == "packy-code"
    assert document.new_connection_id("packy") == "packy-2"
    assert document.new_connection_id("深度求索") == "connection"


def test_requests_require_models_and_normalize_connection_fields() -> None:
    """新建连接必须带至少一个模型、模型互不重名；地址去末尾斜杠，空白字段被拒绝。"""

    request = ModelConnectionCreateRequest(
        name="本地模型",
        base_url="https://example.com/v1/",
        api_key="secret",
        models=[{"model_name": " demo-model ", "context_window_k": 128}],
    )

    assert request.base_url == "https://example.com/v1"
    assert request.models[0].to_entry() == ModelEntry(model_name="demo-model", context_window_k=128)
    for models in ([], [{"model_name": "m"}]):
        with pytest.raises(ValueError):
            ModelConnectionCreateRequest(name="n", base_url="u", api_key="k", models=models)
    with pytest.raises(ValueError, match="duplicate"):
        ModelConfigsAddRequest(
            models=[
                {"model_name": "m", "context_window_k": 8},
                {"model_name": "m", "context_window_k": 16},
            ]
        )
    with pytest.raises(ValueError):
        ModelConfigTestRequest(
            base_url="https://example.com/v1", api_key=" ", model_name="m", context_window_k=8
        )


def test_connection_response_carries_model_ids_and_key_for_local_edit_form() -> None:
    connection = ModelConnectionEntry.model_validate_json(
        json.dumps(_connection(models=[{"model_name": "demo-model", "context_window_k": 64}]))
    )

    response = ModelConnectionResponse.from_entry(connection)

    assert response.api_key == "k"
    assert [(m.config_id, m.connection_id, m.context_window_k) for m in response.models] == [
        ("packy/demo-model", "packy", 64)
    ]


@pytest.mark.asyncio
async def test_draft_connection_test_uses_the_submitted_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未保存测试必须把「连接 + 模型」解析为 ``ResolvedModel`` 后送入真实探活入口。"""

    captured: list[ResolvedModel] = []

    async def fake_probe(model_settings: ResolvedModel, **kwargs: object) -> ModelProbeOutcome:
        captured.append(model_settings)
        return ModelProbeOutcome(ok=True, elapsed_ms=12)

    monkeypatch.setattr(
        "app.service.model_config.model_config_service.probe_chat_model", fake_probe
    )

    result = await ModelConfigService().test_draft(
        base_url="https://example.com/v1",
        api_key="secret",
        model=ModelEntry(
            model_name="demo-model",
            context_window_k=128,
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_image=True,
        ),
    )

    assert (result.success, result.elapsed_ms) == (True, 12)
    settings = captured[0]
    assert (settings.base_url, settings.api_key, settings.model_name) == (
        "https://example.com/v1",
        "secret",
        "demo-model",
    )
    assert settings.context_window_k == 128
    assert settings.supports_thinking and settings.supports_reasoning_effort
    assert settings.supports_image


@pytest.mark.asyncio
async def test_connection_failure_exposes_stable_code_and_controlled_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探活失败必须透出稳定 error_code 与受控文案，且不得泄漏探活细节。"""

    async def fake_probe(model_settings: ResolvedModel, **kwargs: object) -> ModelProbeOutcome:
        return ModelProbeOutcome(
            ok=False,
            elapsed_ms=608,
            failure_kind=ModelProbeFailureKind.INVALID_RESPONSE,
            detail="<!doctype html>",
        )

    monkeypatch.setattr(
        "app.service.model_config.model_config_service.probe_chat_model", fake_probe
    )
    result = await ModelConfigService().test_draft(
        base_url="https://www.packyapi.ai",
        api_key="secret",
        model=ModelEntry(model_name="qwen3.8-flash", context_window_k=1000),
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
