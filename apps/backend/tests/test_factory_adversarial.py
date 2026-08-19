"""factory 对抗性边界测试（独立测试 Agent 新增）。

目标：挖掘 ``build_chat_model`` / ``_build_proxy_client`` 在配置边界下的缺陷。
- 代理构造：仅 URL、URL+Key、两者都无；
- thinking_request 非 dict 时的容错；
- 各厂商 thinking 注入仅在 supports_thinking 时；
- capability 默认值透传（drop_params / max_retries / timeout）；
- 未知厂商类型回退 custom 语义。
"""

from typing import Any, cast
from unittest.mock import patch

from langchain_core.language_models import BaseChatModel

from app.core.llm.factory import _build_proxy_client, build_chat_model
from app.core.llm.model_settings import ModelSettings


def _capture_build_kwargs(model_name: str, model_settings: ModelSettings | None = None) -> dict:
    captured: dict = {}

    class FakeChat:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    with patch("app.core.llm.factory.ChatLiteLLM", FakeChat):
        build_chat_model(model_name, model_settings)
    return captured


def test_proxy_client_none_when_no_url() -> None:
    """未配置 WEB_PROXY_URL → _build_proxy_client 返回 None。"""
    from app.config.settings import Settings

    old_url = Settings.WEB_PROXY_URL
    old_key = Settings.WEB_PROXY_API_KEY
    try:
        Settings.WEB_PROXY_URL = None
        Settings.WEB_PROXY_API_KEY = None
        assert _build_proxy_client() is None
    finally:
        Settings.WEB_PROXY_URL = old_url
        Settings.WEB_PROXY_API_KEY = old_key


def test_proxy_client_configured_with_url_and_auth() -> None:
    """配置 URL+API_KEY → AsyncClient 收到 proxy URL 与 Bearer Authorization 头。"""
    from app.config.settings import Settings

    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    old_url = Settings.WEB_PROXY_URL
    old_key = Settings.WEB_PROXY_API_KEY
    try:
        Settings.WEB_PROXY_URL = "http://proxy:8080"
        Settings.WEB_PROXY_API_KEY = "secret-token"
        with patch("app.core.llm.factory.httpx.AsyncClient", FakeClient):
            client = _build_proxy_client()
        assert client is not None
        assert captured["proxy"] == "http://proxy:8080"
        assert captured["headers"] == {"Authorization": "Bearer secret-token"}
        assert captured["timeout"] == Settings.WEB_REQUEST_TIMEOUT_SECONDS
    finally:
        Settings.WEB_PROXY_URL = old_url
        Settings.WEB_PROXY_API_KEY = old_key


def test_proxy_client_url_without_key_no_auth_header() -> None:
    """配置 URL 但无 API_KEY → 不注入 Authorization 头。"""
    from app.config.settings import Settings

    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    old_url = Settings.WEB_PROXY_URL
    old_key = Settings.WEB_PROXY_API_KEY
    try:
        Settings.WEB_PROXY_URL = "http://proxy:8080"
        Settings.WEB_PROXY_API_KEY = None
        with patch("app.core.llm.factory.httpx.AsyncClient", FakeClient):
            client = _build_proxy_client()
        assert client is not None
        assert captured["headers"] == {}
    finally:
        Settings.WEB_PROXY_URL = old_url
        Settings.WEB_PROXY_API_KEY = old_key


def test_thinking_request_non_dict_used_as_is() -> None:
    """thinking_request 为非 dict（防御）→ 原样塞入 model_kwargs（不抛）。"""
    kwargs = _capture_build_kwargs(
        "deepseek/deepseek-v4-flash",
        ModelSettings(thinking=True, thinking_request="enabled"),
    )
    # 现状：thinking_request or {...} → 字符串被直接使用
    assert kwargs["model_kwargs"]["thinking"] == "enabled"


def test_thinking_injection_not_for_non_thinking_provider() -> None:
    """openai-compatible 不支持 thinking → 即便 thinking=True 也不注入。"""
    kwargs = _capture_build_kwargs(
        "openai/gpt-4o", ModelSettings(thinking=True, provider_type="openai-compatible")
    )
    assert kwargs["model_kwargs"] == {}


def test_capability_default_timeout_used_when_not_specified() -> None:
    """未指定 timeout_seconds → 用 capability.default_timeout_seconds（deepseek=120）。"""
    kwargs = _capture_build_kwargs("deepseek/deepseek-v4-flash", ModelSettings())
    assert kwargs["request_timeout"] == 120.0


def test_capability_timeout_override() -> None:
    """ModelSettings.timeout_seconds 覆盖 capability 默认。"""
    kwargs = _capture_build_kwargs(
        "deepseek/deepseek-v4-flash", ModelSettings(timeout_seconds=30.0)
    )
    assert kwargs["request_timeout"] == 30.0


def test_unknown_provider_type_falls_back_to_custom() -> None:
    """未知厂商类型 → 回退 custom 语义（prefix=None、drop_params=True、max_retries=2）。"""
    kwargs = _capture_build_kwargs(
        "unknown-vendor/xyz", ModelSettings(provider_type="unknown-vendor")
    )
    assert kwargs["drop_params"] is True
    assert kwargs["max_retries"] == 2


def test_unknown_provider_model_settings_custom_semantics() -> None:
    """未知厂商且 thinking 开启 → 因 custom 不支持 thinking 不注入。"""
    kwargs = _capture_build_kwargs(
        "unknown-vendor/xyz", ModelSettings(thinking=True, provider_type="custom")
    )
    assert kwargs["model_kwargs"] == {}


def test_anthropic_thinking_request_budget_tokens() -> None:
    """anthropic 支持 thinking → thinking_request 预算参数透传。"""
    kwargs = _capture_build_kwargs(
        "anthropic/claude",
        ModelSettings(
            thinking=True,
            provider_type="anthropic",
            thinking_request={"type": "enabled", "budget_tokens": 4096},
        ),
    )
    assert kwargs["model_kwargs"]["thinking"] == {
        "type": "enabled",
        "budget_tokens": 4096,
    }


def test_build_chat_model_returns_BaseChatModel_instance() -> None:
    """真实构建（未 mock）返回 BaseChatModel 子类实例（构造契约）。"""
    model = build_chat_model("deepseek/deepseek-v4-flash", ModelSettings())
    assert isinstance(model, BaseChatModel)
    assert cast(Any, model).api_key is None
