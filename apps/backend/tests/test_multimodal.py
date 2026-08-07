"""Qwen（通义千问）LLM 适配层单元测试（纯逻辑，不触数据库）。

覆盖：
- ``ModelSettings`` 新增 ``provider`` 字段的序列化 / 反序列化；
- ``QwenProvider.build`` 构造出携带正确 base_url / api_key / thinking 的 chat model；
- ``build_chat_model`` 按 ``provider`` 路由到 ``QwenProvider`` / ``DeepSeekProvider``；
- 缺 API Key 时统一回退 fake 模型（不触发 Provider 构造）。

不覆盖：真实 LLM 网络调用、多模态内容块 / 持久化（后续接入，另立测试）。
"""

from __future__ import annotations

from app.core.llm.factory import build_chat_model
from app.core.llm.llm_provider.qwen_provider import QwenProvider
from app.core.llm.model_settings import ModelSettings

# ----------------------------------------------------------------------
# ModelSettings.provider
# ----------------------------------------------------------------------


def test_model_settings_provider_round_trip():
    ms = ModelSettings(
        provider="qwen", base_url="https://x.example", api_key_env="DASHSCOPE_API_KEY"
    )
    data = ms.to_dict()
    assert data["provider"] == "qwen"
    assert ModelSettings.from_dict(data).provider == "qwen"


def test_model_settings_provider_default_none():
    assert ModelSettings().provider is None


# ----------------------------------------------------------------------
# QwenProvider.build
# ----------------------------------------------------------------------


def test_qwen_provider_build_constructs_model(monkeypatch):
    """构造的 ChatOpenAI 携带正确 base_url / api_key / thinking。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-fake")
    ms = ModelSettings(
        provider="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        thinking=True,
    )
    model = QwenProvider().build("qwen3.7-plus", ms)
    assert model.model_name == "qwen3.7-plus"
    assert model.openai_api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert model.openai_api_key.get_secret_value() == "sk-fake"
    assert model.streaming is True
    assert model.model_kwargs.get("thinking") == {"type": "enabled"}


def test_qwen_provider_build_default_base_url(monkeypatch):
    """base_url 未配置时回落到环境变量 / 默认 DashScope URL。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-fake")
    monkeypatch.delenv("QWEN_BASE_URL", raising=False)
    ms = ModelSettings(provider="qwen", api_key_env="DASHSCOPE_API_KEY")
    model = QwenProvider().build("qwen3.7-plus", ms)
    assert model.openai_api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_qwen_provider_build_env_base_url_override(monkeypatch):
    """``QWEN_BASE_URL`` 环境变量可覆盖默认端点（用于配额/计费隔离）。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-fake")
    monkeypatch.setenv(
        "QWEN_BASE_URL",
        "https://ws-xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    ms = ModelSettings(provider="qwen", api_key_env="DASHSCOPE_API_KEY")
    model = QwenProvider().build("qwen3.7-plus", ms)
    assert model.openai_api_base == (
        "https://ws-xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )


# ----------------------------------------------------------------------
# build_chat_model 按 provider 路由
# ----------------------------------------------------------------------


def test_build_chat_model_routes_to_qwen(monkeypatch):
    """provider=qwen 且 API Key 齐备时路由到 QwenProvider。"""
    called: list[str] = []

    class _FakeQwen:
        def build(self, model_name: str, model_settings=None):
            called.append("qwen")
            return "qwen-model"

    monkeypatch.setattr("app.core.llm.factory.QwenProvider", lambda: _FakeQwen())
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-fake")

    result = build_chat_model(
        "qwen3.7-plus",
        ModelSettings(provider="qwen", api_key_env="DASHSCOPE_API_KEY"),
    )
    assert result == "qwen-model"
    assert called == ["qwen"]


def test_build_chat_model_routes_to_deepseek_by_default(monkeypatch):
    """provider 缺省（None）且 API Key 齐备时路由到 DeepSeekProvider（向后兼容）。"""
    called: list[str] = []

    class _FakeDeepSeek:
        def build(self, model_name: str, model_settings=None):
            called.append("deepseek")
            return "ds-model"

    monkeypatch.setattr("app.core.llm.factory.DeepSeekProvider", lambda: _FakeDeepSeek())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake")

    result = build_chat_model(
        "deepseek-v4-flash",
        ModelSettings(api_key_env="DEEPSEEK_API_KEY"),
    )
    assert result == "ds-model"
    assert called == ["deepseek"]


def test_build_chat_model_no_key_falls_back_to_fake(monkeypatch):
    """缺 API Key 时（即使 provider=qwen）统一回退 fake 模型，不触发 Provider 构造。"""
    called: list[str] = []

    class _FakeQwen:
        def build(self, model_name: str, model_settings=None):
            called.append("qwen")
            return "qwen-model"

    monkeypatch.setattr("app.core.llm.factory.QwenProvider", lambda: _FakeQwen())
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    result = build_chat_model(
        "qwen3.7-plus",
        ModelSettings(provider="qwen", api_key_env="DASHSCOPE_API_KEY"),
    )
    assert called == []
    assert result.__class__.__name__ == "GenericFakeChatModel"
