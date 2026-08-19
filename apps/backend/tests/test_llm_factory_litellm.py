"""ChatLiteLLM 单一收口工厂与模型事实目录的单元测试。

覆盖「全面切 litellm 库模式」改造后的新行为契约：

- ``build_chat_model``：``api_key`` 由解析链从 DB 透传（唯一事实来源，构建期不再读
  环境变量、不再抛缺 Key 的 ``ValueError``）；``api_key`` 为 None 时交 litellm 按前缀
  解析（构造不抛）；``thinking=True`` → ``model_kwargs={"thinking": {"type":
  "enabled"}}``；采样参数与 ``base_url`` 正确透传。**全部用例仅构造模型对象，不触发
  任何真实 HTTP 请求**。
- ``ModelCatalog.max_context_window``：带 provider 前缀名与裸名均命中同一事实表项。
- profile 迁移：``AgentProfile`` 默认 ``model_name`` 带前缀；``define_agents`` 各内置
  profile 不再持有 ``base_url`` 字段（移入 ``ModelSettings``），``model_name`` 均带前缀，
  且**不携带 Key 明文**（``ModelSettings.api_key`` 默认 None，Key 完全由 DB 提供）。
"""

from typing import Any, cast
from unittest.mock import patch

from langchain_core.language_models import BaseChatModel

from app.core.agents import define_agents
from app.core.agents.agent_profile import AgentProfile
from app.core.llm.factory import build_chat_model
from app.core.llm.model_catalog import ModelCatalog
from app.core.llm.model_settings import ModelSettings


def _capture_build_kwargs(model_name: str, model_settings: ModelSettings | None = None) -> dict:
    """mock ChatLiteLLM 构造并返回捕获到的构造参数（用于断言接入参数透传）。"""
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    with patch("app.core.llm.factory.ChatLiteLLM", FakeChat):
        build_chat_model(model_name, model_settings)
    return captured


# --------------------------------------------------------------------------- #
# build_chat_model：缺 Key / 未配置 Key / thinking / 参数透传
# --------------------------------------------------------------------------- #
def test_build_chat_model_passes_api_key_plaintext() -> None:
    """api_key 明文经 ModelSettings 透传至 ChatLiteLLM（DB 唯一事实来源，构建期不读 env）。"""
    model = build_chat_model(
        "deepseek/deepseek-v4-flash",
        ModelSettings(api_key="sk-test-plaintext"),
    )

    assert isinstance(model, BaseChatModel)
    # ChatLiteLLM 的 api_key 为运行时实例属性（BaseChatModel 静态类型未声明）：
    # isinstance 收窄后再 cast(Any) 显式转义，通过 strict mypy 同时保留契约断言。
    raw = cast(Any, model)
    assert raw.api_key == "sk-test-plaintext"


def test_build_chat_model_without_api_key_constructs_safely() -> None:
    """api_key 为 None（未配置）时构造不抛，返回 BaseChatModel（交 litellm 按前缀解析）。"""
    model = build_chat_model("deepseek/deepseek-v4-flash", ModelSettings())

    assert isinstance(model, BaseChatModel)
    raw = cast(Any, model)
    assert raw.api_key is None


def test_build_chat_model_default_settings_uses_default_model_name() -> None:
    """不传 model_settings 时（缺省 None）同样构造成功且 model 名原样透传。"""
    model = build_chat_model("deepseek/deepseek-v4-flash")

    assert isinstance(model, BaseChatModel)
    assert model.model == "deepseek/deepseek-v4-flash"


def test_build_chat_model_thinking_enabled_sets_model_kwargs() -> None:
    """thinking=True 时 ChatLiteLLM 收到 model_kwargs={"thinking": {"type": "enabled"}}。"""
    model = build_chat_model(
        "deepseek/deepseek-v4-flash",
        ModelSettings(thinking=True),
    )

    assert model.model_kwargs == {"thinking": {"type": "enabled"}}


def test_build_chat_model_thinking_disabled_keeps_model_kwargs_empty() -> None:
    """thinking 未启用（None/False）时不注入 thinking 配置。"""
    model = build_chat_model(
        "deepseek/deepseek-v4-flash",
        ModelSettings(thinking=False),
    )

    assert model.model_kwargs == {}


def test_build_chat_model_passes_sampling_and_base_url() -> None:
    """temperature/top_p/max_tokens/base_url 透传至 ChatLiteLLM 构造参数。"""
    model = build_chat_model(
        "deepseek/deepseek-v4-flash",
        ModelSettings(
            temperature=0.3,
            top_p=0.9,
            max_tokens=2048,
            base_url="http://localhost:8001/v1",
        ),
    )

    assert model.temperature == 0.3
    assert model.top_p == 0.9
    assert model.max_tokens == 2048
    assert model.api_base == "http://localhost:8001/v1"


def test_build_chat_model_returns_new_instance_per_call() -> None:
    """每次调用返回新实例，可安全复用（不共享可变状态）。"""
    first = build_chat_model("deepseek/deepseek-v4-flash", ModelSettings())
    second = build_chat_model("deepseek/deepseek-v4-flash", ModelSettings())

    assert first is not second


# --------------------------------------------------------------------------- #
# build_chat_model：能力透传（设计文档阶段 2，mock ChatLiteLLM 捕获构造参数）
# --------------------------------------------------------------------------- #
def test_factory_passes_max_retries_and_drop_params_from_capability() -> None:
    """deepseek 能力默认 max_retries=2、drop_params=True 透传至 ChatLiteLLM。"""
    kwargs = _capture_build_kwargs("deepseek/deepseek-v4-flash", ModelSettings())

    assert kwargs["max_retries"] == 2
    assert kwargs["drop_params"] is True
    assert kwargs["model"] == "deepseek/deepseek-v4-flash"


def test_factory_model_settings_override_capability_max_retries() -> None:
    """ModelSettings.max_retries 覆盖能力默认值。"""
    kwargs = _capture_build_kwargs(
        "deepseek/deepseek-v4-flash",
        ModelSettings(max_retries=5),
    )

    assert kwargs["max_retries"] == 5


def test_factory_thinking_injected_only_when_capability_supports_thinking() -> None:
    """thinking 注入仅在 capability.supports_thinking 且 config.thinking 时进行。"""
    # deepseek 支持 thinking：thinking=True → 注入 thinking 参数
    deepseek_kwargs = _capture_build_kwargs(
        "deepseek/deepseek-v4-flash", ModelSettings(thinking=True)
    )
    assert deepseek_kwargs["model_kwargs"]["thinking"] == {"type": "enabled"}

    # azure 不支持 thinking：即使 thinking=True 也不注入（避免 400）
    azure_kwargs = _capture_build_kwargs(
        "azure/xxx", ModelSettings(thinking=True, provider_type="azure")
    )
    assert azure_kwargs["model_kwargs"] == {}


def test_factory_thinking_request_overrides_default_thinking_payload() -> None:
    """thinking_request（厂商专属开启参数）优先于通用 {"type":"enabled"}。"""
    kwargs = _capture_build_kwargs(
        "deepseek/deepseek-v4-flash",
        ModelSettings(
            thinking=True,
            thinking_request={"type": "enabled", "budget_tokens": 4096},
        ),
    )

    assert kwargs["model_kwargs"]["thinking"] == {
        "type": "enabled",
        "budget_tokens": 4096,
    }


def test_factory_capability_prefix_fallback_without_provider_type() -> None:
    """未传 provider_type 时按 model_name 前缀回退解析能力（openai-compatible → openai/）。"""
    kwargs = _capture_build_kwargs(
        "openai/gpt-4o", ModelSettings(thinking=False, drop_params=True)
    )

    assert kwargs["model"] == "openai/gpt-4o"
    # openai-compatible 默认 drop_params=False，ModelSettings 显式 True 覆盖
    assert kwargs["drop_params"] is True


# --------------------------------------------------------------------------- #
# ModelCatalog：前缀归一化
# --------------------------------------------------------------------------- #
def test_model_catalog_normalizes_provider_prefix() -> None:
    """带 provider 前缀名与裸名均命中同一事实表项（1M）。"""
    assert ModelCatalog.max_context_window("deepseek/deepseek-v4-flash") == 1_000_000
    assert ModelCatalog.max_context_window("deepseek-v4-flash") == 1_000_000


def test_model_catalog_normalizes_unknown_prefixed_model_to_fallback() -> None:
    """未收录的带前缀模型名归一化后仍查不到，返回兜底窗口。"""
    assert ModelCatalog.max_context_window("unknown-provider/unknown-model") == 128_000


# --------------------------------------------------------------------------- #
# profile 迁移：model_name 带前缀、base_url 字段移除
# --------------------------------------------------------------------------- #
def test_agent_profile_default_model_name_is_none() -> None:
    """AgentProfile 默认 model_name 为 None（2026-08-18 决议：内置 profile 不内置默认模型）。"""
    profile = AgentProfile(
        agent_id="t",
        role="t",
        description="t",
        allowed_tools=[],
    )

    assert profile.model_name is None


def test_builtin_profiles_have_no_default_model_and_no_key() -> None:
    """define_agents 各内置 profile：model_name 默认 None、无 base_url 字段、不携带 Key 明文。"""
    profiles = [
        define_agents.developer_agent(),
        define_agents.reviewer_agent(),
        define_agents.analyst_agent(),
        define_agents.test_agent(),
        define_agents.coder_agent(),
    ]

    for profile in profiles:
        # 2026-08-18 决议：内置 profile 不内置默认模型（str|None）
        assert profile.model_name is None
        # base_url 已移入 ModelSettings，profile 本体不再持有该字段
        assert not hasattr(profile, "base_url")
        # 内置 profile 不携带 Key 明文，Key 完全由 DB（providers.api_key）提供
        assert profile.model_settings.api_key is None
