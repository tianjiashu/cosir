"""factory 单元测试：确保构建出的 ChatOpenAI 默认开启真正的逐 token 流式。

回归防护：build_chat_model 必须实例化 ``ChatOpenAI`` 且 ``streaming=True``，否则
LangChain 的 astream 退化成 ainvoke + 单次 yield，每个模型 step 只产出 1 个整块 chunk，
前端无流式感。

测试通过 monkeypatch ``get_provider_service`` 注入最小 provider 桩，避免依赖真实 DB，
聚焦验证「收口类为 ChatOpenAI + 流式开启」契约。
"""

from unittest.mock import MagicMock

import pytest
from langchain_openai import ChatOpenAI

from app.core.agents.model_settings import ModelSettings
from app.llm_provider.model_factory import build_chat_model


class _FakeProvider:
    """最小 provider 桩：仅暴露 build_chat_model 所需的字段。"""

    name = "deepseek"
    api_key = "test-key"
    base_url = "https://api.deepseek.com/v1"


class _FakeProviderCapability:
    """最小 ProviderCapability 桩：提供能力清单与默认端点。"""

    default_base_url = "https://api.deepseek.com/v1"
    models = ("deepseek-v4-flash",)


@pytest.fixture
def fake_provider_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """用桩替换 get_provider_service 与 ProviderCapability，使测试零外部依赖。"""

    fake = MagicMock()
    fake.get_provider.return_value = _FakeProvider()
    monkeypatch.setattr(
        "app.llm_provider.model_factory.get_provider_service", lambda: fake
    )
    monkeypatch.setattr(
        "app.llm_provider.model_factory.ProviderCapability",
        _FakeProviderCapability,
    )


def test_build_chat_model_returns_chat_openai(
    fake_provider_service: None,
) -> None:
    """构建的模型必须是 ChatOpenAI 实例。"""

    model = build_chat_model(
        product_id=1,
        model_name="deepseek/deepseek-v4-flash",
        model_settings=ModelSettings(stream=True),
    )

    assert isinstance(model, ChatOpenAI)


def test_build_chat_model_enables_streaming(
    fake_provider_service: None,
) -> None:
    """构建的模型必须 streaming=True，否则 astream 不会逐 token 流式。"""

    model = build_chat_model(
        product_id=1,
        model_name="deepseek/deepseek-v4-flash",
        model_settings=ModelSettings(stream=True),
    )

    assert getattr(model, "streaming", False) is True


def test_build_chat_model_strips_provider_prefix(
    fake_provider_service: None,
) -> None:
    """model_name 的厂商前缀必须被剥离，ChatOpenAI 不认 provider/ 前缀。"""

    model = build_chat_model(
        product_id=1,
        model_name="deepseek/deepseek-v4-flash",
        model_settings=ModelSettings(stream=True),
    )

    assert model.model_name == "deepseek-v4-flash"
