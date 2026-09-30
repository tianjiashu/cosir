"""模型目录发现服务测试。"""

import pytest

from app.service.model_config.model_discovery_service import (
    ModelDiscoveryService,
    _build_models_url,
    _extract_model_ids,
)


def test_model_discovery_url_preserves_base_path() -> None:
    """追加 models 时必须保留 OpenAI 兼容地址通常包含的 /v1 前缀。"""

    assert _build_models_url("https://example.com/v1/") == "https://example.com/v1/models"
    assert _build_models_url("https://example.com/custom/openai") == (
        "https://example.com/custom/openai/models"
    )
    assert _build_models_url("example.com/v1") is None


def test_model_discovery_extracts_non_empty_unique_model_ids() -> None:
    """目录投影只接受字符串 ID，并按远端顺序去重。"""

    payload = {
        "object": "list",
        "data": [
            {"id": "model-a"},
            {"id": " model-b "},
            {"id": "model-a"},
            {"id": ""},
            {"id": 123},
        ],
    }

    assert _extract_model_ids(payload) == ["model-a", "model-b"]
    assert _extract_model_ids({"data": "invalid"}) == []


@pytest.mark.asyncio
async def test_discover_requests_models_with_bearer_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """发现服务请求保留 Base URL 路径并使用 Bearer 鉴权。"""

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return {"object": "list", "data": [{"id": "model-a"}]}

    class FakeClient:
        def __init__(self) -> None:
            self.url: str | None = None
            self.headers: dict[str, str] | None = None

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
            self.url = url
            self.headers = headers
            return FakeResponse()

    client = FakeClient()
    monkeypatch.setattr(
        "app.service.model_config.model_discovery_service.build_proxy_async_client",
        lambda **kwargs: client,
    )

    models = await ModelDiscoveryService().discover(
        base_url="https://example.com/v1",
        api_key="secret",
    )

    assert models == ["model-a"]
    assert client.url == "https://example.com/v1/models"
    assert client.headers == {"Authorization": "Bearer secret"}
