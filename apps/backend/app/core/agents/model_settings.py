"""单个 Agent 的模型覆盖配置值对象。

只承载该 Agent 的模型覆盖配置（生成参数 + 推理强度 + 可选接入覆盖），不参与模型构建。
所有字段均为可选覆盖项：未提供（``None``）时由 ``ProviderCapability`` 注册表与
``Constant.LLM``（请求超时 / 重试 / 种子）提供缺省值，``ModelSettings`` 仅覆盖显式给出的字段。

序列化字段清单由 ``_fields()`` 从 dataclass 实际字段推导（唯一事实源），
不再手工维护——手工清单曾与实际字段漂移（缺 ``response_format``、多 ``base_url``/
``api_key``），导致 JSON 往返静默丢字段。

用户手写配置的严格校验也归本模块：``ModelSettings.from_json`` 是「JSON 覆盖项对象 →
``ModelSettings``」的唯一入口（未知字段、类型不符、非有限数值一律拒绝），字段类型从
dataclass 注解推导，避免校验规则与值对象漂移。
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, get_args, get_type_hints

# 序列化权威字段清单：与 ModelSettings 全部字段一一对应，新增字段须同步追加。
# 由 ``_fields()`` 从 dataclass 实际字段推导，避免注解漂移（此前手工维护清单
# 缺 ``response_format``、多 ``base_url``/``api_key``，导致 JSON 往返静默丢字段）。
_LEGACY_EXTRA_FIELDS = ()


def _fields() -> tuple[str, ...]:
    """返回序列化权威字段清单（唯一事实源：dataclass 实际声明的字段）。

    参数:
        无。

    返回:
        按声明顺序排列的字段名元组。

    异常:
        无。

    副作用:
        无。
    """

    return tuple(f.name for f in fields(ModelSettings)) + _LEGACY_EXTRA_FIELDS


class ModelSettingsError(ValueError):
    """表示 JSON ``model_settings`` 覆盖项不满足本值对象的字段契约。

    由 :meth:`ModelSettings.from_json` 抛出（未知字段、值类型不符、非有限数值、构造失败）。
    消息只描述字段级原因、不含文件路径——配置来源路径由调用方
    （``AgentProfile.vaild_agent_profile``）写进 ``agent_profile_config_invalid`` 日志的
    ``file`` 字段。
    """


def _accepted_value_types() -> dict[str, tuple[type, ...]]:
    """返回 ``ModelSettings`` 每个覆盖字段可接受的 JSON 值类型。

    类型从 ``ModelSettings`` 的字段注解推导（唯一事实源），不手工维护字段名到类型的
    映射表，避免新增字段时校验与值对象漂移。

    参数:
        无。

    返回:
        字段名 → 去掉 ``None`` 后的注解类型元组。

    异常:
        无。

    副作用:
        无。
    """

    accepted: dict[str, tuple[type, ...]] = {}
    for name, annotation in get_type_hints(ModelSettings).items():
        members = tuple(member for member in get_args(annotation) if member is not type(None))
        accepted[name] = members or (annotation,)
    return accepted


def _accepts_value(value: Any, accepted: tuple[type, ...]) -> bool:
    """判断 JSON 值是否落在字段注解允许的类型内。

    参数:
        value: 从 JSON 反序列化得到的值。
        accepted: 该字段允许的类型元组（由 :func:`_accepted_value_types` 推导）。

    返回:
        值类型合法时为 ``True``。

    异常:
        无。

    副作用:
        无。

    说明:
        ``bool`` 是 ``int`` 的子类，必须优先单独判定；整数字面量对 ``float`` 覆盖项
        视为合法（JSON 数字不区分 ``1`` 与 ``1.0``）。
    """

    if isinstance(value, bool):
        return bool in accepted
    if isinstance(value, int) and float in accepted and int not in accepted:
        return True
    return isinstance(value, accepted)


@dataclass
class ModelSettings:
    """单个 Agent 的模型覆盖配置值对象（声明式覆盖项，不参与模型构建）。

    字段分类：
    - 采样参数：``temperature`` / ``top_p`` / ``max_tokens``；
    - 推理强度：``reasoning_effort``（``low``/``high``/``max``，None 时不注入）；
    - thinking 开关：``thinking``（是否抽取/回传思考块）；
    - 接入与能力覆盖：``provider_type``（注册表键，None 时按 model_name 推导）/
      ``drop_params``（None 时回退 ``ProviderCapability.default_drop_params``）/
      ``stream``（None 时不覆盖）/ ``response_format``（text / json_object，None 时不注入）。

    全局默认值（超时/重试/seed 等）归 ``app.config.constant.Constant.LLM`` 与
    ``ProviderCapability`` 注册表；上下文窗口上限归 ``ModelCapability.context_window``（经
    ``CapabilityService.get_model_context_window`` 解析），此处不为假想需求预留覆盖字段。
    """

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    thinking: bool | None = None
    # 可选厂商类型（注册表键，如 ``deepseek`` / ``azure``）：None 时由
    # ``factory.build_chat_model`` 按 model_name 前缀回退推导。
    provider_type: str | None = None
    # 可选是否丢弃不支持参数覆盖：None 时回退 ``ProviderCapability.default_drop_params``。
    drop_params: bool | None = None
    # 可选流式开关覆盖：None 时不覆盖（沿用运行时默认流式）。
    stream: bool | None = None
    # 可选推理强度覆盖（``low`` / ``high`` / ``max``）：None 时不注入。
    reasoning_effort: str | None = None
    # 可选响应格式覆盖：None 时不注入。text / json_object
    response_format: str | None = None


    @classmethod
    def default_settings(cls) -> "ModelSettings":
        return cls(
            stream = True,
            reasoning_effort="high",
            thinking=True
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可序列化字典（仅含非 ``None`` 字段）。

        参数:
            无。

        返回:
            字段名到值的字典；值为 ``None`` 的覆盖项被省略（表示「不覆盖」）。

        异常:
            无。

        副作用:
            无。
        """

        return {k: getattr(self, k) for k in _fields() if getattr(self, k) is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelSettings":
        """从字典重建，忽略未知键（前向兼容，缺失键沿用字段默认值）。

        参数:
            data: 待反序列化的字典（通常为 :meth:`to_dict` 的产物）。

        返回:
            重建后的 ``ModelSettings`` 实例。

        异常:
            TypeError: 若字典中存在 dataclass 未声明的字段且值无法接受（理论上被
                ``_fields()`` 过滤，不会发生）。

        副作用:
            无。
        """

        return cls(**{k: data[k] for k in _fields() if k in data})

    @classmethod
    def from_json(cls, values: Mapping[str, Any]) -> "ModelSettings":
        """严格校验用户手写 JSON 的 ``model_settings`` 覆盖项并构造实例。

        与 :meth:`from_dict` 的宽松语义相对：本方法服务配置文件输入，未知字段、类型不符、
        非有限数值一律拒绝，且不做隐式类型转换（``"1"`` 不会变成 ``1``，``true`` 不会
        变成 ``1``）。

        参数:
            values: 从 JSON 配置文件反序列化得到的 ``model_settings`` 对象（键为字段名，
                值为 JSON 标量）。允许为空对象，表示不覆盖任何字段。

        返回:
            由覆盖项构造的 ``ModelSettings``；未出现的字段沿用字段默认值。

        异常:
            ModelSettingsError: 含未知字段、值类型不符、出现非有限数值（``NaN`` /
                ``Infinity``），或值对象构造失败。消息为字段级原因，不含文件路径。

        副作用:
            无（不读取文件、不写运行时状态）。
        """
        if values is None or len(values) == 0:
            return ModelSettings.default_settings()

        accepted_types = _accepted_value_types()
        unknown = sorted(set(values) - set(accepted_types))
        if unknown:
            raise ModelSettingsError(f"model_settings 含未知字段: {', '.join(unknown)}")
        for name, value in values.items():
            accepted = accepted_types[name]
            if not _accepts_value(value, accepted):
                expected = "/".join(item.__name__ for item in accepted)
                raise ModelSettingsError(f"model_settings.{name} 必须是 {expected}")
            if isinstance(value, float) and not math.isfinite(value):
                raise ModelSettingsError(f"model_settings.{name} 必须是有限数值")
        try:
            return cls(**values)
        except (TypeError, ValueError) as exc:
            raise ModelSettingsError(f"model_settings 构造失败: {exc}") from exc
