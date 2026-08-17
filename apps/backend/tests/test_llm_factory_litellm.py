"""ChatLiteLLM 单一收口工厂与模型事实目录的单元测试。

覆盖「全面切 litellm 库模式」改造后的新行为契约：

- ``build_chat_model``：缺 Key（``api_key_env`` 配置但环境变量缺失/为空）构建期抛
  ``ValueError``；未配置 Key 时 ``api_key=None`` 交 litellm 按前缀解析（构造不抛）；
  ``thinking=True`` → ``model_kwargs={"thinking": {"type": "enabled"}}``；采样参数与
  ``base_url`` 正确透传。**全部用例仅构造模型对象，不触发任何真实 HTTP 请求**。
- ``ModelCatalog.max_context_window``：带 provider 前缀名与裸名均命中同一事实表项。
- profile 迁移：``AgentProfile`` 默认 ``model_name`` 带前缀；``define_agents`` 各内置
  profile 不再持有 ``base_url`` 字段（移入 ``ModelSettings``），``model_name`` 均带前缀，
  且配置了 ``DEEPSEEK_API_KEY`` 作为 ``api_key_env``。
"""

import pytest
from langchain_core.language_models import BaseChatModel

from app.core.agents import define_agents
from app.core.agents.agent_profile import AgentProfile
from app.core.llm.factory import build_chat_model
from app.core.llm.model_catalog import ModelCatalog
from app.core.llm.model_settings import ModelSettings


# --------------------------------------------------------------------------- #
# build_chat_model：缺 Key / 未配置 Key / thinking / 参数透传
# --------------------------------------------------------------------------- #
def test_build_chat_model_missing_api_key_env_raises(monkeypatch) -> None:
    """api_key_env 显式配置但环境变量缺失时，构建期抛 ValueError 而非静默回退。"""
    monkeypatch.delenv("NONEXISTENT_KEY_ENV", raising=False)

    with pytest.raises(ValueError, match="NONEXISTENT_KEY_ENV"):
        build_chat_model(
            "deepseek/deepseek-v4-flash",
            ModelSettings(api_key_env="NONEXISTENT_KEY_ENV"),
        )


def test_build_chat_model_without_api_key_env_constructs_safely() -> None:
    """未配置 api_key_env 时构造不抛，返回 BaseChatModel 且 api_key 为 None（交 litellm 解析）。"""
    model = build_chat_model("deepseek/deepseek-v4-flash", ModelSettings())

    assert isinstance(model, BaseChatModel)
    assert model.api_key is None


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
def test_agent_profile_default_model_name_has_provider_prefix() -> None:
    """AgentProfile 默认 model_name 为带 provider 前缀的 deepseek/deepseek-v4-flash。"""
    profile = AgentProfile(
        agent_id="t",
        role="t",
        description="t",
        allowed_tools=[],
    )

    assert profile.model_name == "deepseek/deepseek-v4-flash"


def test_builtin_profiles_have_prefixed_model_name_and_no_base_url_field() -> None:
    """define_agents 各内置 profile：model_name 带前缀、无 base_url 字段、Key 走 DEEPSEEK_API_KEY。"""
    profiles = [
        define_agents.developer_agent(),
        define_agents.reviewer_agent(),
        define_agents.analyst_agent(),
        define_agents.test_agent(),
        define_agents.coder_agent(),
    ]

    for profile in profiles:
        assert profile.model_name == "deepseek/deepseek-v4-flash"
        # base_url 已移入 ModelSettings，profile 本体不再持有该字段
        assert not hasattr(profile, "base_url")
        assert profile.model_settings.api_key_env == "DEEPSEEK_API_KEY"
