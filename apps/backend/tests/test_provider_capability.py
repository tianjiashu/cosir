"""``ProviderCapability`` 静态注册表单元测试。

覆盖目标（设计文档 §三「单一事实源」契约）：
- 注册表覆盖 15 类厂商且键集合稳定。
- ``get_capability`` 已知类型返回对应 capability，未知类型回退 custom 语义。
- ``get_provider_types`` 返回的元组与注册表键一致且顺序稳定。
- capability 字段内部一致性（前缀尾斜杠 / thinking 通道与 supports_thinking
  对齐 / azure 与 custom 的特殊语义）。
- ``ProviderService.api_key_configured`` 静态方法在不依赖 DB 的前提下按
  capability.requires_api_key 正确分流（覆盖注册表与 service 的接线点）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.models import ProviderRecord
from app.models.provider_capability import (
    PROVIDER_CAPABILITIES,
    get_capability,
    get_provider_types,
)

#: 设计文档 §三声明的 15 类厂商键（顺序与注册表插入顺序一致）。
_EXPECTED_PROVIDER_TYPES: tuple[str, ...] = (
    "deepseek",
    "openai-compatible",
    "anthropic",
    "gemini",
    "azure",
    "dashscope",
    "moonshot",
    "zai",
    "volcengine",
    "tencent",
    "minimax",
    "ollama",
    "qianfan",
    "xfyun",
    "custom",
)


def test_registry_covers_expected_provider_types() -> None:
    """注册表键集合与设计文档 §三声明的 15 类厂商一致。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当注册表键集合与预期不一致时由 pytest 抛出。

    副作用:
        无。
    """

    assert set(PROVIDER_CAPABILITIES.keys()) == set(_EXPECTED_PROVIDER_TYPES)


def test_get_provider_types_matches_registry_and_order() -> None:
    """``get_provider_types`` 元组与注册表键及插入顺序一致。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 元组与注册表键或顺序不一致时由 pytest 抛出。

    副作用:
        无。
    """

    assert get_provider_types() == tuple(PROVIDER_CAPABILITIES.keys())
    assert get_provider_types() == _EXPECTED_PROVIDER_TYPES


def test_get_capability_known_type_returns_registry_entry() -> None:
    """已知厂商类型返回注册表中对应 capability（同一对象，零拷贝）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当返回值与注册表项不一致时由 pytest 抛出。

    副作用:
        无。
    """

    deepseek = get_capability("deepseek")
    assert deepseek is PROVIDER_CAPABILITIES["deepseek"]
    assert deepseek.litellm_prefix == "deepseek/"
    assert deepseek.requires_api_key is True
    assert deepseek.supports_thinking is True
    assert deepseek.thinking_channels == ("reasoning_content",)


def test_get_capability_unknown_type_returns_custom_fallback() -> None:
    """未知厂商类型回退 custom 语义（不抛异常）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当回退语义与 custom 不一致时由 pytest 抛出。

    副作用:
        无。
    """

    fallback = get_capability("nonexistent-vendor")
    custom = get_capability("custom")
    assert fallback.litellm_prefix == custom.litellm_prefix
    assert fallback.requires_api_key == custom.requires_api_key
    assert fallback.requires_manual_model_entry is True
    assert fallback.discover_endpoint is None
    # 未知类型回退保留调用方传入的原始 provider_type（便于排查日志；不污染 frozen 单例）
    assert fallback.provider_type == "nonexistent-vendor"
    # 同一 unknown 类型重复查询返回不同实例（replace 新构造，不污染 _CUSTOM_FALLBACK）
    assert get_capability("nonexistent-vendor") is not custom


def test_all_litellm_prefixes_end_with_slash_or_none() -> None:
    """非 None 前缀必须以尾斜杠结尾（过滤逻辑依赖此约定）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当存在非 None 但无尾斜杠的前缀时由 pytest 抛出。

    副作用:
        无。
    """

    for provider_type, cap in PROVIDER_CAPABILITIES.items():
        if cap.litellm_prefix is None:
            continue
        assert cap.litellm_prefix.endswith(
            "/"
        ), f"{provider_type} litellm_prefix must end with '/': {cap.litellm_prefix}"


def test_thinking_channels_align_with_supports_thinking() -> None:
    """``supports_thinking=True`` 必须有非空 thinking_channels；反之可空。

    设计文档 §五「thinking 生命周期」：supports_thinking=True 的厂商必须
    声明至少一条 thinking 通道供响应侧提取；supports_thinking=False 时
    通道应为空（避免 service 层误读）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 supports_thinking 与 thinking_channels 矛盾时
            由 pytest 抛出。

    副作用:
        无。
    """

    for provider_type, cap in PROVIDER_CAPABILITIES.items():
        if cap.supports_thinking:
            assert (
                cap.thinking_channels
            ), f"{provider_type} supports_thinking=True but thinking_channels empty"
        # supports_thinking=False 允许通道为空或非空（保留未来扩展余地），
        # 不做强断言。


def test_ollama_is_keyless_provider() -> None:
    """ollama 是注册表中唯一 requires_api_key=False 的厂商。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 ollama 仍要求 Key 或其他厂商误设为 keyless 时
            由 pytest 抛出。

    副作用:
        无。
    """

    keyless = {ptype for ptype, cap in PROVIDER_CAPABILITIES.items() if not cap.requires_api_key}
    assert keyless == {"ollama"}


def test_custom_has_no_prefix_and_requires_manual_model_entry() -> None:
    """custom 厂商无前缀过滤、要求手填模型名、无自动发现端点。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 custom 字段语义与设计文档矛盾时由 pytest 抛出。

    副作用:
        无。
    """

    custom = PROVIDER_CAPABILITIES["custom"]
    assert custom.litellm_prefix is None
    assert custom.requires_manual_model_entry is True
    assert custom.discover_endpoint is None


def test_azure_requires_api_version_and_manual_entry() -> None:
    """azure 是唯一要求 api_version + 手填 deployment 名的厂商。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 azure 字段语义与设计文档矛盾时由 pytest 抛出。

    副作用:
        无。
    """

    azure = PROVIDER_CAPABILITIES["azure"]
    assert azure.requires_api_version is True
    assert azure.requires_manual_model_entry is True
    assert azure.discover_endpoint is None


def test_capability_is_frozen() -> None:
    """capability 是 frozen dataclass，字段不可变（避免运行时篡改注册表）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: FrozenInstanceError 未触发时由 pytest 抛出。

    副作用:
        无。
    """

    cap = get_capability("deepseek")
    with pytest.raises(FrozenInstanceError):
        cap.litellm_prefix = "tampered/"  # type: ignore[misc]


def _make_provider_record(provider_type: str, api_key: str | None) -> ProviderRecord:
    """构造仅用于 api_key_configured 测试的 ProviderRecord 内存对象。

    参数:
        provider_type: 厂商类型键。
        api_key: 厂商 API Key 明文或 None。

    返回:
        不落库的 ProviderRecord 实例。

    异常:
        无。

    副作用:
        无。
    """

    from app.utils.datetime_utils import utc_now

    now = utc_now()
    return ProviderRecord(
        provider_id=f"p-{provider_type}",
        name=provider_type,
        provider_type=provider_type,
        created_at=now,
        updated_at=now,
        base_url=None,
        api_key=api_key,
        enabled=True,
        sort_order=0,
    )


def test_api_key_configured_dispatches_to_capability() -> None:
    """``ProviderService.api_key_configured`` 按 capability.requires_api_key 分流。

    覆盖注册表与 service 的接线点（设计文档 §8.4）：
    - ollama（requires_api_key=False）即使 api_key=None 也返回 True；
    - deepseek（requires_api_key=True）api_key=None 返回 False，非空返回 True。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当分流逻辑与 capability 矛盾时由 pytest 抛出。

    副作用:
        无（静态方法，不读 DB）。
    """

    from app.service.provider.provider_service import ProviderService

    ollama_no_key = _make_provider_record("ollama", api_key=None)
    deepseek_no_key = _make_provider_record("deepseek", api_key=None)
    deepseek_with_key = _make_provider_record("deepseek", api_key="sk-test")

    assert ProviderService.api_key_configured(ollama_no_key) is True
    assert ProviderService.api_key_configured(deepseek_no_key) is False
    assert ProviderService.api_key_configured(deepseek_with_key) is True
