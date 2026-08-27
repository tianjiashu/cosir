"""
模型静态能力元数据：以 ``model_capabilities.json`` 为唯一真相源。

唯一职责：集中维护各模型的静态能力事实（上下文窗口、最大输出、是否支持思考 /
图像 / 视频、是否需要 reasoning_content、推理强度档位等），供模型选择、能力探测、
上下文占用计算的查表。能力是模型的事实属性，不应散落在 AgentProfile 构造或前端
硬编码里重复抄写；新增模型时在此 JSON 追加一行即可，计算代码无需改动。

事实源边界：除 ``custom`` 类模型（用户自建、能力由用户自行声明，本包不收录）外，
所有模型能力必须且只能来自 ``model_capabilities.json``；JSON 中不存在的模型名经
``get_capability`` 返回保守默认副本（不抛错，由调用方决定如何提示用户补全 JSON）。

与 ``model_catalog.ModelCatalog`` 的关系：本包是模型「静态能力」的唯一事实源；
``ModelCatalog`` 负责「上下文窗口查表 + litellm 目录懒查回填」这一独立链路，二者
互补，本包不侵入其窗口解析职责。

不负责：全局软上限（归 Settings）、实际窗口的 min 计算（归 context_window_resolver）、
模型配置的加载（归 ModelSettings）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 模型能力 JSON 数据源（与注册表同目录，只读，不修改该文件）。
_MODEL_JSON_PATH: Path = Path(__file__).resolve().parent / "model_capabilities.json"


def load_model_json() -> dict[str, dict[str, Any]]:
    """读取 ``model_capabilities.json`` 模型能力数据源（只读，不修改该文件）。

    参数:
        无。

    返回:
        以模型名键索引的原始条目字典；文件缺失或解析失败时返回空字典
        （此时所有模型均回退保守默认，不阻断启动）。

    异常:
        无（文件缺失 / JSON 非法均吞掉并返回空字典）。

    副作用:
        无（纯读取，不写日志以避免 leaf 层反向依赖 ``config.logging``）。
    """

    try:
        with _MODEL_JSON_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


#: JSON 数据源解析结果（模块级单次加载，避免每次查询重复读盘）。
_MODEL_JSON_DATA: dict[str, dict[str, Any]] = load_model_json()


@dataclass(frozen=True, slots=True)
class ReasoningEffortCapability:
    """模型推理强度能力（不可变值对象，JSON ``supports_reasoning_effort`` 的运行时表示）。

    字段:
        supported: 是否支持推理强度调节。
        effort_map: 支持的强度档位映射（如 ``{"low": "low", "high": "high", "max": "max"}``）；
            不支持时为空字典。键为内部档位名，值为透传给模型的原始档位标识。
    """

    supported: bool = False
    effort_map: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelCapability:
    """单个模型的静态能力元数据（不可变值对象，JSON 唯一真相源的运行时表示）。

    实例由 ``get_capability`` 从 ``model_capabilities.json`` 条目转换而来；JSON 中
    不存在的模型名（或非 ``custom`` 的未知模型）经 ``get_capability`` 返回保守默认
    副本。

    字段:
        model_name: 模型名键（与 JSON 外层键对齐）。
        context_window: 最大上下文窗口 token 数。
        max_output_tokens: 单次最大输出 token 数。
        supports_thinking: 是否支持思考模式。
        supports_image: 是否支持图像输入。
        supports_video: 是否支持视频输入。
        need_reasoning_content: 是否需要在请求中透传 reasoning_content。
        reasoning_effort: 推理强度能力（见 ``ReasoningEffortCapability``）。
        is_custom: 是否为用户自建 custom 模型（此类模型本包不收录，走保守默认）。
    """

    model_name: str
    context_window: int = 0
    max_output_tokens: int = 0
    supports_thinking: bool = False
    supports_image: bool = False
    supports_video: bool = False
    need_reasoning_content: bool = False
    reasoning_effort: ReasoningEffortCapability = field(
        default_factory=ReasoningEffortCapability
    )

    @staticmethod
    def get_capability(model_name: str) -> ModelCapability:
        """按模型名查询能力元数据（JSON 为唯一真相源；缺失回退保守默认）。

        这是从 JSON 构造 ``ModelCapability`` 的唯一入口。推理强度档位位于 JSON 的
        ``supports_reasoning_effort`` 子对象内（键 ``effort_map``），需逐层读取并做
        类型兜底，避免把子对象整体当布尔误判为 ``supported=True``。

        参数:
            model_name: 模型名键（与 JSON 外层键对齐）。

        返回:
            命中时返回字段完整的 ``ModelCapability``；未命中时返回保守默认实例
            （所有能力置 False、空档位表、窗口/输出置 0），不抛异常。

        异常:
            无（永不抛——未知模型回退而非报错，由调用方决定如何提示用户补全 JSON）。

        副作用:
            无。
        """

        raw = _MODEL_JSON_DATA.get(model_name)
        if raw is None:
            return ModelCapability(model_name=model_name)
        effort_raw = raw.get("supports_reasoning_effort", {})
        if not isinstance(effort_raw, dict):
            effort_raw = {}
        effort_map_raw = effort_raw.get("effort_map", {})
        if not isinstance(effort_map_raw, dict):
            effort_map_raw = {}
        effort = ReasoningEffortCapability(
            supported=bool(effort_raw.get("supported", False)),
            effort_map=dict(effort_map_raw),
        )
        return ModelCapability(
            model_name=model_name,
            context_window=int(raw.get("context_window", 0)),
            max_output_tokens=int(raw.get("max_output_tokens", 0)),
            supports_thinking=bool(raw.get("supports_thinking", False)),
            supports_image=bool(raw.get("supports_image", False)),
            supports_video=bool(raw.get("supports_video", False)),
            need_reasoning_content=bool(raw.get("need_reasoning_content", False)),
            reasoning_effort=effort,
        )
