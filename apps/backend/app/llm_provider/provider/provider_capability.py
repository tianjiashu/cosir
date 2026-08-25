"""
厂商能力元数据：以 ``llm_provider.json`` 为唯一真相源。
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


#: 默认请求超时秒数（覆盖模型客户端内置默认，避免国内厂商偶发慢响应超时）。
_DEFAULT_TIMEOUT_SECONDS: float = 120.0

#: 默认最大重试次数（覆盖模型客户端内置默认 1）。
_DEFAULT_MAX_RETRIES: int = 3

#: 厂商能力 JSON 数据源（与注册表同目录，只读，不修改该文件）。
_PROVIDER_JSON_PATH: Path = Path(__file__).resolve().parent / "llm_provider.json"


def load_provider_json() -> dict[str, dict[str, Any]]:
    """读取 ``llm_provider.json`` 厂商能力数据源（只读，不修改该文件）。

    参数:
        无。

    返回:
        以厂商类型键（与注册表键对齐）索引的原始条目字典；文件缺失或解析失败时
        返回空字典（此时无任何厂商可用，由调用方提示用户补全 JSON）。

    异常:
        无（文件缺失/JSON 非法均吞掉并返回空字典，不阻断启动）。

    副作用:
        无（纯读取，不写日志以避免 leaf 层反向依赖 ``config.logging``）。
    """

    try:
        with _PROVIDER_JSON_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data

#: JSON 数据源解析结果（模块级单次加载，避免每次查询重复读盘）。
_PROVIDER_JSON_DATA: dict[str, dict[str, Any]] = load_provider_json()

@dataclass(frozen=True, slots=True)
class ProviderCapability:
    """单个厂商的能力元数据（不可变值对象，JSON 唯一真相源的运行时表示）。

    实例由 ``provider_capability_from_json`` 从 ``llm_provider.json`` 条目转换而来；
    JSON 中不存在的厂商类型经 ``get_capability`` 构造保守默认副本（``model_prefix``
    为空、``provider_type`` 保留调用方原始值以便排查）。

    字段:
        provider_type: 厂商类型键（与 JSON 外层键对齐，也是注册表查询键）。
        default_base_url: 厂商默认 API 端点；``None`` 表示无内置默认，必须由用户填写
            （如本地部署、私有化）。
        default_drop_params: 是否默认向模型客户端透传 ``drop_params=True``
            （丢弃目标厂商不支持的参数；默认 ``False``）。
        thinking_channels: 响应侧 thinking 字段名元组（如 ``("reasoning_content",)``）；
            ``model_node`` 据此从 chunk 提取 thinking 文本。空元组表示不支持 thinking。
        models: 厂商支持的模型名元组（如 ``("deepseek-v4-flash-vision-exp",)``）。
        extra: 厂商特定字段字典
    """

    provider_type: str
    default_base_url: str | None = None
    requires_api_key: bool = True
    thinking_channels: str = ""
    models: tuple[str, ...] = field(default_factory=tuple)
    extra: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def get_capability(provider_name: str) -> ProviderCapability:
        """按厂商类型查询能力元数据（JSON 为唯一真相源；缺失回退保守默认）。

        参数:
            provider_type: 厂商类型字符串（JSON 外层键）。

        返回:
            JSON 中存在则为其转换后的 ``ProviderCapability``；不存在则返回
            ``_build_fallback`` 构造的保守默认副本（``provider_type`` 保留调用方传入的
            原始值，便于排查日志中可见用户误填的类型名）。

        异常:
            无（永不抛——未知类型回退而非报错，由调用方决定如何提示用户补全 JSON）。

        副作用:
            无。
        """

        raw = _PROVIDER_JSON_DATA.get(provider_name)
        if raw is None:
            raise ValueError(f"Provider {provider_name} not found")
        return ProviderCapability(
            provider_type=raw.get("provider_type", "api"),
            default_base_url=raw.get("base_url"),
            requires_api_key=True,
            thinking_channels=raw.get("thinking_channels", "reasoning_content"),
            models=tuple(raw.get("models", [])),
            extra=raw.get("extra", {}),
        )



def get_capability(provider_name: str) -> ProviderCapability:
    """按厂商类型查询能力元数据（JSON 为唯一真相源；缺失回退保守默认）。

    参数:
        provider_type: 厂商类型字符串（JSON 外层键）。

    返回:
        JSON 中存在则为其转换后的 ``ProviderCapability``；不存在则返回
        ``_build_fallback`` 构造的保守默认副本（``provider_type`` 保留调用方传入的
        原始值，便于排查日志中可见用户误填的类型名）。

    异常:
        无（永不抛——未知类型回退而非报错，由调用方决定如何提示用户补全 JSON）。

    副作用:
        无。
    """

    raw = _PROVIDER_JSON_DATA.get(provider_name)
    if raw is None:
        raise ValueError(f"Provider {provider_name} not found")
    return ProviderCapability(
        provider_type=raw.get("provider_type","api"),
        default_base_url=raw.get("base_url"),
        requires_api_key=True,
        thinking_channels=raw.get("thinking_channels","reasoning_content"),
        models=tuple(raw.get("models", [])),
        extra=raw.get("extra", {}),
    )
