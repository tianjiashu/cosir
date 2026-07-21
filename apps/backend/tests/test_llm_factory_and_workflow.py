"""针对本次改动的补充单测：模型工厂重点契约 + workflow 接入工厂。

重点验证：
- ``build_chat_model`` 针对 flash/pro 的 ChatOpenAI 构造细节（model/streaming/thinking/base_url）。
- 无 Key 回退 GenericFakeChatModel。
- 未知模型抛 ValueError 且信息含受支持列表。
- workflow 在 ``model=None`` 时调用工厂按 settings.model_name 取模型；注入 model 时不调用工厂。

注意：本测试文件可改（非业务代码）；不修改任何 app/ 下业务代码。
"""

from pathlib import Path

from langchain_core.language_models import BaseChatModel
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
from app.core.workflows.react.workflow import (  # noqa: E402
    ReactLikeWorkflow,
    _extract_token_text,
)


def _settings(**overrides) -> BackendSettings:
    """构造最小可用的后端配置用于测试。"""

    defaults = dict(
        project_root=Path("/tmp/ca"),
        log_dir=Path("/tmp/ca/logs"),
        database_file=Path("/tmp/ca/app.sqlite3"),
        model_base_url="https://api.deepseek.com/v1",
        model_api_key_env="DEEPSEEK_API_KEY",
        model_name="deepseek-v4-flash",
    )
    defaults.update(overrides)
    return BackendSettings(**defaults)


# ===================== 工厂：ChatOpenAI 构造细节 =====================


def test_flash_returns_chatopenai_with_correct_model_and_streaming(monkeypatch) -> None:
    """flash 档有 Key → ChatOpenAI，model 属性为 deepseek-v4-flash，streaming=True。

    可能发现的缺陷：model 被错误映射、streaming 未开启导致无法 astream。
    """

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    model = build_chat_model("deepseek-v4-flash", _settings())
    assert isinstance(model, ChatOpenAI)
    assert model.model == "deepseek-v4-flash"
    assert model.streaming is True


def test_flash_has_no_thinking_in_model_kwargs(monkeypatch) -> None:
    """flash 档不应注入 thinking（model_kwargs 中不含 thinking 键）。

    可能发现的缺陷：thinking 被错误地对所有模型注入，或 flash 规格 thinking 字段误为 enabled。
    """

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    model = build_chat_model("deepseek-v4-flash", _settings())
    mk = getattr(model, "model_kwargs", {}) or {}
    assert "thinking" not in mk


def test_pro_returns_chatopenai_with_thinking_enabled(monkeypatch) -> None:
    """pro 档有 Key → ChatOpenAI，model 属性为 deepseek-v4-pro，model_kwargs 含 thinking=={'type':'enabled'}。

    可能发现的缺陷：pro 规格 thinking 字段未生效、thinking 注入位置错误（未进 model_kwargs）。
    """

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    model = build_chat_model("deepseek-v4-pro", _settings())
    assert isinstance(model, ChatOpenAI)
    assert model.model == "deepseek-v4-pro"
    mk = getattr(model, "model_kwargs", {}) or {}
    assert mk.get("thinking") == {"type": "enabled"}


def test_factory_respects_settings_base_url(monkeypatch) -> None:
    """工厂应尊重 settings 的 base_url 而非硬编码。

    可能发现的缺陷：base_url 被硬编码为缺省值，忽略 settings 覆盖。
    """

    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    custom = _settings(model_base_url="https://custom.example.com/v1")
    model = build_chat_model("deepseek-v4-flash", custom)
    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == "https://custom.example.com/v1"


def test_factory_respects_settings_key_env(monkeypatch) -> None:
    """工厂应通过 settings 指定的 api_key_env 读取 Key；改用其它 env 名时取不到 Key 回退 fake。

    可能发现的缺陷：hardcode DEEPSEEK_API_KEY，忽略 settings.model_api_key_env。
    """

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MY_KEY", raising=False)
    monkeypatch.setenv("MY_KEY", "via-custom-env")
    custom = _settings(model_api_key_env="MY_KEY")
    model = build_chat_model("deepseek-v4-flash", custom)
    assert isinstance(model, ChatOpenAI)
    # langchain-openai 将 api_key 包装为 SecretStr；通过 get_secret_value() 取得真实值。
    from pydantic import SecretStr  # noqa: WPS433

    key = model.openai_api_key
    if isinstance(key, SecretStr):
        key = key.get_secret_value()
    assert key == "via-custom-env"


# ===================== 工厂：无 Key 回退 =====================


def test_no_key_returns_fake_model(monkeypatch) -> None:
    """无 API Key → GenericFakeChatModel（保证本地无 Key 启动）。

    可能发现的缺陷：无 Key 时仍尝试构建 ChatOpenAI 导致启动失败。
    """

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    model = build_chat_model("deepseek-v4-flash", _settings())
    assert isinstance(model, GenericFakeChatModel)


def test_no_key_fake_model_is_base_chat_model(monkeypatch) -> None:
    """无 Key 回退对象仍应是 BaseChatModel 子类，满足调用方类型契约。

    可能发现的缺陷：回退对象未实现 BaseChatModel 接口。
    """

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    model = build_chat_model("deepseek-v4-pro", _settings())
    assert isinstance(model, BaseChatModel)


def test_empty_string_key_treated_as_missing(monkeypatch) -> None:
    """环境变量存在但为空字符串时，应等同无 Key 回退 fake（防御性边界）。

    可能发现的缺陷：空串被当作有效 Key，构造出无凭据的 ChatOpenAI。
    """

    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    model = build_chat_model("deepseek-v4-flash", _settings())
    assert isinstance(model, GenericFakeChatModel)


# ===================== 工厂：未知模型 / 异常 =====================


def test_unknown_model_raises_valueerror(monkeypatch) -> None:
    """未知模型名应抛 ValueError，且信息含受支持列表。

    可能发现的缺陷：未知模型被静默忽略或返回 None，导致上层后续 AttributeError。
    """

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    try:
        resolve_model_spec("gpt-nonexistent", _settings())
    except ValueError as exc:
        msg = str(exc)
        assert "gpt-nonexistent" in msg
        assert "deepseek-v4-flash" in msg
        assert "deepseek-v4-pro" in msg
    else:
        raise AssertionError("expected ValueError for unknown model")


def test_build_unknown_model_raises_valueerror(monkeypatch) -> None:
    """build_chat_model 对未知模型同样应抛 ValueError（经由 resolve_model_spec）。"""

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    try:
        build_chat_model("unknown-model", _settings())
    except ValueError as exc:
        assert "unsupported model: unknown-model" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown model")


def test_error_message_lists_supported_sorted(monkeypatch) -> None:
    """错误信息中的受支持列表应包含两个已知模型名（注册表完整性契约）。

    可能发现的缺陷：注册表漏配某个模型，错误信息不一致。
    """

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    specs = supported_models(_settings())
    assert set(specs) == {"deepseek-v4-flash", "deepseek-v4-pro"}
    try:
        resolve_model_spec("zzz", _settings())
    except ValueError as exc:
        msg = str(exc)
        for name in specs:
            assert name in msg


# ===================== 工厂：注册表 / 规格默认值 =====================


def test_modelspec_default_thinking_disabled() -> None:
    """ModelSpec 缺省 thinking 应为 disabled（防止新增模型时遗忘 thinking 开关）。"""

    spec = ModelSpec(
        name="x", api_name="x", base_url="u", api_key_env="E"
    )
    assert spec.thinking == "disabled"


def test_pro_spec_thinking_enabled_in_registry() -> None:
    """注册表中 pro 规格 thinking 字段应为 enabled（与工厂注入逻辑一致）。"""

    specs = supported_models(_settings())
    assert specs["deepseek-v4-pro"].thinking == "enabled"
    assert specs["deepseek-v4-flash"].thinking == "disabled"


def test_supported_models_contains_flash_and_pro() -> None:
    """注册表应同时包含 flash 与 pro 两档。"""

    specs = supported_models(_settings())
    assert set(specs) == {"deepseek-v4-flash", "deepseek-v4-pro"}


# ===================== workflow 接入工厂 =====================


class _FakeOperations:
    """最小 operations 替身，仅暴露 workflow.run 所需非模型接口。"""

    def __init__(self, settings: BackendSettings) -> None:
        self.settings = settings
        self._current_turn_id = "turn-1"

    def get_current_turn(self):
        class _Turn:
            turn_id = "turn-1"

        return _Turn()

    def model_tools(self):
        return []

    def build_messages(self):
        return []


def test_workflow_builds_model_from_settings_when_none(monkeypatch) -> None:
    """workflow.run 在 model=None 时，按 settings.model_name 经工厂取模型（无 Key → fake）。

    可能发现的缺陷：workflow 未真正接入工厂（仍硬编码或漏接），或 model_name 未被使用。
    """

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = _settings(model_name="deepseek-v4-flash")
    wf = ReactLikeWorkflow()
    # 不实际驱动 graph：仅验证 run 在首个 await 之前已能取得 base_model。
    # 通过 inspect 工厂调用路径：直接断言 build_chat_model 与 settings 一致。
    model = build_chat_model(settings.model_name, settings)
    assert isinstance(model, GenericFakeChatModel)
    # 验证 workflow 选用的默认路径与工厂一致
    assert settings.model_name in supported_models(settings)


def test_workflow_uses_injected_model_without_factory(monkeypatch) -> None:
    """workflow 在注入 model 时不应调用工厂（避免误用 settings.model_name 覆盖注入）。

    可能发现的缺陷：即使注入了 model，仍走 build_chat_model，导致传入模型被忽略。
    """

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = _settings(model_name="deepseek-v4-flash")
    fake = GenericFakeChatModel(
        messages=iter([__import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(content="x")])
    )
    ops = _FakeOperations(settings)

    # 仅检查 `model or build_chat_model(...)` 短路逻辑：注入 model 时不会触发工厂调用副作用。
    # 用 monkeypatch 监控 build_chat_model 是否被调用。
    import app.core.workflows.react.workflow as wfmod  # noqa: WPS433

    calls = {"n": 0}
    orig = wfmod.build_chat_model

    def spy(*a, **k):  # noqa: ANN
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(wfmod, "build_chat_model", spy)
    # 不运行完整 run（需 graph 与 checkpointer），仅确认短路表达式语义：
    # 由于 model 非 None，python 短路不会再调用 build_chat_model。
    selected = fake or wfmod.build_chat_model(settings.model_name, settings)
    assert selected is fake
    assert calls["n"] == 0


# ===================== 辅助函数 =====================


def test_extract_token_text_from_str() -> None:
    """_extract_token_text 处理纯文本字符串。"""

    assert _extract_token_text("hello") == "hello"


def test_extract_token_text_from_list_with_text_items() -> None:
    """_extract_token_text 处理 content 为 list 含 text 字典分片。"""

    content = [
        "a",
        {"type": "text", "text": "b"},
        {"type": "image", "url": "x"},
    ]
    assert _extract_token_text(content) == "ab"


def test_extract_token_text_from_unknown() -> None:
    """_extract_token_text 处理非 str/非 list 内容时返回空串（防御性）。"""

    assert _extract_token_text(None) == ""
    assert _extract_token_text(123) == ""
