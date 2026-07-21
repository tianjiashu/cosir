"""模型工厂单测：验证按模型名取模型、flash/pro 差异屏蔽、无 Key 回退。"""

from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_openai import ChatOpenAI

from tests.conftest_stub_log import install_log_crud_stub

install_log_crud_stub()

from app.config.settings import BackendSettings  # noqa: E402
from app.core.llm.factory import (  # noqa: E402
    ModelSpec,
    build_chat_model,
    resolve_model_spec,
    supported_models,
)


def _settings() -> BackendSettings:
    """构造最小可用的后端配置用于测试。"""

    return BackendSettings(
        project_root=Path("/tmp/ca"),
        log_dir=Path("/tmp/ca/logs"),
        database_file=Path("/tmp/ca/app.sqlite3"),
        model_base_url="https://api.deepseek.com/v1",
        model_api_key_env="DEEPSEEK_API_KEY",
        model_name="deepseek-v4-flash",
    )


def test_supported_models_contains_flash_and_pro() -> None:
    """注册表应同时包含 deepseek-v4-flash 与 deepseek-v4-pro。"""

    specs = supported_models(_settings())
    assert set(specs) == {"deepseek-v4-flash", "deepseek-v4-pro"}
    assert isinstance(specs["deepseek-v4-flash"], ModelSpec)


def test_resolve_unknown_model_raises() -> None:
    """未知模型名称应抛出 ValueError 并列出受支持项。"""

    try:
        resolve_model_spec("gpt-nonexistent", _settings())
    except ValueError as exc:
        assert "unsupported model: gpt-nonexistent" in str(exc)
        assert "deepseek-v4-flash" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown model")


def test_build_without_key_returns_fake(monkeypatch) -> None:
    """缺 API Key 时回退到 GenericFakeChatModel，保证本地无 Key 启动。"""

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    model = build_chat_model("deepseek-v4-flash", _settings())
    assert isinstance(model, GenericFakeChatModel)


def test_build_flash_returns_chatopenai_without_thinking(monkeypatch) -> None:
    """flash 档应构建 ChatOpenAI，且不注入 thinking 参数。"""

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    model = build_chat_model("deepseek-v4-flash", _settings())
    assert isinstance(model, ChatOpenAI)
    assert model.model == "deepseek-v4-flash"
    assert model.openai_api_base == "https://api.deepseek.com/v1"
    assert model.streaming is True
    assert getattr(model, "model_kwargs", {}).get("thinking") is None


def test_build_pro_enables_thinking(monkeypatch) -> None:
    """pro 档应构建 ChatOpenAI，并注入 thinking=enabled。"""

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    model = build_chat_model("deepseek-v4-pro", _settings())
    assert isinstance(model, ChatOpenAI)
    assert model.model == "deepseek-v4-pro"
    assert getattr(model, "model_kwargs", {}).get("thinking") == {"type": "enabled"}


def test_build_uses_settings_base_url_and_key_env(monkeypatch) -> None:
    """工厂应尊重 settings 的 base_url 与 api_key_env，而非硬编码。"""

    settings = _settings()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    model = build_chat_model("deepseek-v4-flash", settings)
    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == settings.model_base_url
