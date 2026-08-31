"""
厂商能力元数据：以 ``llm_provider.json`` 为唯一真相源。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

_SUPPORT_PROVIDERS: set[str] = set(_PROVIDER_JSON_DATA.keys())


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
        extra_body: 厂商自定义请求参数（透传至 ``ChatOpenAI.extra_body``）。用于承载
            OpenAI 标准 API 之外的厂商私有参数（如 LM Studio 的 ``ttl``、vLLM 的
            ``use_beam_search``、各厂商私有开关）；空字典表示无自定义参数。注意：
            **不要**在此放入 OpenAI 标准参数或 ``model`` 等已由 ``ChatOpenAI`` 顶层
            字段处理的键，否则会与主请求体冲突。
        disabled_params: 该厂商/模型应屏蔽的 ``ChatOpenAI`` 客户端参数（透传至
            ``ChatOpenAI.disabled_params``）。用于老模型不支持某些自动注入参数（如
            ``parallel_tool_calls`` / ``strict``）时软屏蔽，避免 ``with_structured_output``
            等内置方法注入导致 400。取值格式见 langchain 文档：``{"param": None}`` 完全禁用、
            ``{"param": ["v1", "v2"]}`` 仅禁用指定值。空字典表示无屏蔽。

            生成长度上限参数名（``max_tokens`` vs ``max_completion_tokens``）即由本字段控制：
            工厂会同时设置二者，最终由 ``disabled_params`` 屏蔽其中不需要的一个
            （如 ``{"max_tokens": None}`` 表示仅用 ``max_completion_tokens``）。

        vision_input_format: 视觉输入格式（路由经验值，非模型事实）。本期仅支持
            ``"openai_url"``（DeepSeek 等 OpenAI 兼容厂商走 ``image_url`` 形式）；
            其它厂商（Anthropic / Gemini）的视觉格式本期未实现，默认空串由调用方转
            ``VisionNotSupportedError``。

    注 ``llm_provider.json`` 顶层键完整去向（避免后续误判字段归属）：
    ``provider_type`` / ``base_url`` / ``models`` / ``thinking_channels`` 已建模；
    ``extra_body`` 透传至请求体（见上）；``disabled_params`` 透传至 ``ChatOpenAI.disabled_params``
    （见上）；``error_code`` 为厂商错误码→文案映射，属于错误处理层事实数据，**不**经
    ``ProviderCapability`` 透传，由错误归一模块独立读取。
    """

    provider_type: str
    default_base_url: str | None = None
    requires_api_key: bool = True
    thinking_channel: str = ""
    models: tuple[str, ...] = field(default_factory=tuple)
    extra_body: dict[str, Any] = field(default_factory=dict)
    disabled_params: dict[str, Any] = field(default_factory=dict)
    vision_input_format: str = ""

    @staticmethod
    def get_capability(provider_name: str) -> ProviderCapability:
        """按厂商类型查询能力元数据（JSON 为唯一真相源；未注册则抛错，不静默兜底）。

        参数:
            provider_name: 厂商类型字符串（JSON 外层键）。

        返回:
            JSON 中存在则为其转换后的 ``ProviderCapability``。

        异常:
            ValueError: 当 ``provider_name`` 未在 ``llm_provider.json`` 注册时抛出；未注册的
                厂商无法给出可信的 ``base_url`` 等接入元数据，交由调用方收敛为构建期失败，
                而非返回误导性的保守默认。

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
            thinking_channel=raw.get("thinking_channel", "reasoning_content"),
            models=tuple(raw.get("models", [])),
            extra_body=raw.get("extra_body", {}),
            disabled_params=raw.get("disabled_params", {}),
            vision_input_format=raw.get("vision_input_format", ""),
        )
