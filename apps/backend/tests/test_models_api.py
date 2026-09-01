from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.dependencies import get_provider_service
from app.app import app
from app.llm_provider.capability.model_capability import (
    ModelCapability,
    ReasoningEffortCapability,
)
from app.llm_provider.capability.provider_capability import ProviderCapability


class FakeProviderService:
    def list_providers(self, enabled: bool = True) -> list[SimpleNamespace]:
        assert enabled is True
        return [
            SimpleNamespace(id=1, name="provider-a"),
            SimpleNamespace(id=2, name="provider-b"),
        ]


def test_list_models_returns_provider_groups_and_capabilities(monkeypatch) -> None:
    def fake_provider_capability(provider_name: str) -> ProviderCapability:
        models = {
            "provider-a": ("model-a",),
            "provider-b": ("model-b",),
        }
        return ProviderCapability(provider_type=provider_name, models=models[provider_name])

    def fake_model_capability(model_name: str) -> ModelCapability:
        return ModelCapability(
            model_name=model_name,
            supports_thinking=model_name == "model-a",
            supports_image=model_name == "model-b",
            supports_video=False,
            reasoning_effort=ReasoningEffortCapability(
                supported=model_name == "model-a",
                effort_map={"low": "low", "high": "medium", "max": "xhigh"}
                if model_name == "model-a"
                else {},
            ),
        )

    monkeypatch.setattr(ProviderCapability, "get_capability", fake_provider_capability)
    monkeypatch.setattr(ModelCapability, "get_capability", fake_model_capability)
    app.dependency_overrides[get_provider_service] = FakeProviderService
    try:
        with TestClient(app) as client:
            response = client.get("/models")

        assert response.status_code == 200
        assert response.json() == [
            {
                "provider_id": 1,
                "provider_name": "provider-a",
                "models": [
                    {
                        "model_name": "model-a",
                        "supports_thinking": True,
                        "supports_image": False,
                        "supports_video": False,
                        "supports_reasoning_effort": True,
                    }
                ],
            },
            {
                "provider_id": 2,
                "provider_name": "provider-b",
                "models": [
                    {
                        "model_name": "model-b",
                        "supports_thinking": False,
                        "supports_image": True,
                        "supports_video": False,
                        "supports_reasoning_effort": False,
                    }
                ],
            },
        ]
    finally:
        app.dependency_overrides.clear()
