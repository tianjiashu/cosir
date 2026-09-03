"""factory 单元测试：确保构建出的 ChatOpenAI 默认开启真正的逐 token 流式。

回归防护：build_chat_model 必须实例化 ``ChatOpenAI`` 且 ``streaming=True``，否则
LangChain 的 astream 退化成 ainvoke + 单次 yield，每个模型 step 只产出 1 个整块 chunk，
前端无流式感。

测试通过 monkeypatch ``get_provider_service`` 注入最小 provider 桩，避免依赖真实 DB，
聚焦验证「收口类为 ChatOpenAI + 流式开启」契约。
"""

from typing import ClassVar
from unittest.mock import MagicMock

import pytest
from langchain_openai import ChatOpenAI

from app.core.agents.model_settings import ModelSettings
from app.core.llm_provider.model_factory import build_chat_model


class _FakeProvider:
    """最小 provider 桩：仅暴露 build_chat_model 所需的字段。"""

    name = "deepseek"
    api_key = "test-key"
    base_url = "https://api.deepseek.com/v1"


class _FakeProviderCapability:
    """最小 ProviderCapability 桩：提供能力清单与默认端点。

    需与生产 ``ProviderCapability`` 的调用契约一致：``model_factory`` 经
    ``ProviderCapability.get_capability(provider.name)`` 取实例后再读实例属性，
    故本桩必须提供同签名的 ``get_capability`` 静态方法（返回带所需属性的对象），
    仅定义类属性会在调用时抛 ``AttributeError``。
    """

    provider_type = "deepseek"
    default_base_url = "https://api.deepseek.com/v1"
    # 与 ``llm_provider.json`` 中 deepseek 条目的形态一致：**不含厂商前缀的纯净模型名**
    # （实测为 deepseek-v4-flash / deepseek-v4-flash-vision-exp / deepseek-v4-pro）。
    # 生产 ``model_factory`` 校验 ``model_name in provider_capability.models``，
    # 清单形态不匹配会在断言前就抛 ValueError。
    models = ("deepseek-v4-flash",)
    requires_api_key = True
    thinking_channel = "reasoning_content"
    extra_body: ClassVar[dict[str, object]] = {}
    disabled_params: ClassVar[dict[str, object]] = {}
    vision_input_format = "openai_url"

    @staticmethod
    def get_capability(provider_name: str) -> "_FakeProviderCapability":
        """返回本桩实例（忽略厂商名，测试只使用 deepseek 一种）。"""

        return _FakeProviderCapability()


@pytest.fixture
def fake_provider_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """用桩替换 get_provider_service 与 ProviderCapability，使测试零外部依赖。"""

    fake = MagicMock()
    fake.get_provider.return_value = _FakeProvider()
    monkeypatch.setattr("app.core.llm_provider.model_factory.get_provider_service", lambda: fake)
    monkeypatch.setattr(
        "app.core.llm_provider.model_factory.ProviderCapability",
        _FakeProviderCapability,
    )


def test_build_chat_model_returns_chat_openai(
    fake_provider_service: None,
) -> None:
    """构建的模型必须是 ChatOpenAI 实例。"""

    model = build_chat_model(
        provider_id=1,
        model_name="deepseek-v4-flash",
        model_settings=ModelSettings(stream=True),
    )

    assert isinstance(model, ChatOpenAI)


def test_build_chat_model_enables_streaming(
    fake_provider_service: None,
) -> None:
    """构建的模型必须 streaming=True，否则 astream 不会逐 token 流式。"""

    model = build_chat_model(
        provider_id=1,
        model_name="deepseek-v4-flash",
        model_settings=ModelSettings(stream=True),
    )

    assert getattr(model, "streaming", False) is True


def test_build_chat_model_rejects_prefixed_model_name(
    fake_provider_service: None,
) -> None:
    """带厂商前缀的 ``model_name`` 必须被拒绝——工厂不做前缀剥离。

    契约依据（三者一致，均以**不含前缀的纯净名**为准）：
    - ``build_chat_model`` docstring：``model_name`` 为「注册表/能力清单中的纯净
      模型名，不含厂商前缀」；
    - 校验逻辑 ``model_name not in provider_capability.models`` 抛 ``ValueError``，
      而 ``llm_provider.json`` 中 deepseek 的 models 实测为纯净名
      （``deepseek-v4-flash`` / ``deepseek-v4-flash-vision-exp`` / ``deepseek-v4-pro``）；
    - 当前链路已改用 ``ChatOpenAI`` 直连厂商 OpenAI 兼容端点，路由由 ``base_url``
      决定，不再依赖前缀（本项目已无 litellm 主链路）。

    注：本用例原名 ``test_build_chat_model_strips_provider_prefix``，断言「前缀会被
    剥离后传给底层」。该断言源于 litellm 时代——彼时 ``provider/model`` 前缀是
    litellm 的路由依据，工厂需剥离后再交给 ``ChatLiteLLM``。改造为 ``ChatOpenAI``
    后工厂不再剥离，而是直接按纯净名强校验，故原断言所测行为已不存在，此处改为
    断言真实的拒绝契约。
    """

    with pytest.raises(ValueError, match="not in provider_capability.models"):
        build_chat_model(
            provider_id=1,
            model_name="deepseek/deepseek-v4-flash",
            model_settings=ModelSettings(stream=True),
        )
