"""工具参数的宽容输入适配（strict 校验前置）。

单一职责：在 pydantic **strict** 校验之前，把模型下发的「可无损还原的形态差异」按字段注解
归一为目标类型，并返回归一事实供调用方记日志。

职责边界：
- 负责：按 ``args_model`` 的字段注解做白名单归一，产出 :class:`ArgumentCoercion` 事实。
- 不负责：校验本身（strict 仍是唯一校验路径，见 ``validate_tool_arguments``）、业务与安全
  约束（长度、范围、枚举、路径边界、命令策略）、错误文案、日志落盘。

设计边界（为什么这不是「放松校验」）：
- **strict 不变**：本模块只改「进入校验之前的输入形态」，不改变任何校验规则；白名单之外的
  类型不匹配原样交给 strict 报错，因此「同一参数必然被拒」的确定性语义保持不变。
- 白名单只包含无损、无歧义的转换：``"30"``→30、``30.0``→30、``"true"``→True、
  单值→单元素列表；**不猜语义**——不把 ``""`` 当 None、不把 ``"1"/"0"`` 当布尔、不把数字
  当字符串（那会掩盖 ``path=123`` 这类错传）。
- 容器语义（契约，避免误读）：仅 ``list[T]`` 类型参与容器归一；单值→单元素列表**只对
  非空标量**生效（空串/纯空白不包裹，否则会构造出长度合法但内容为空的列表、绕过
  ``min_length``），``tuple`` 输入在元素无需归一时原样保留（交给 strict 判定「应为列表」），
  嵌套 ``list[list[T]]`` 按元素注解递归归一。
- 未知字段不归一，交给 ``extra="forbid"`` 报错，避免拼写错误被静默丢弃。

事实源：归一的目标类型只来自 ``args_model.model_fields`` 的注解；不解析 JSON Schema，
避免出现第二套类型口径。
"""

from __future__ import annotations

import re
import types
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

__all__ = ["ArgumentCoercion", "coerce_tool_arguments"]

# 白名单形态：整型/浮点字符串、布尔字面量。布尔只认 true/false，避免与整型语义混淆。
_INT_PATTERN = re.compile(r"^[+-]?\d+$")
_FLOAT_PATTERN = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_TRUE_LITERALS = frozenset({"true"})
_FALSE_LITERALS = frozenset({"false"})

# 可参与标量归一的原始值类型（排除 bool：它是 int 的子类，单独判定）。
_SCALAR_SOURCES = (str, int, float)


@dataclass(frozen=True)
class ArgumentCoercion:
    """一次字段级归一的可观测事实（**不含参数值**，避免把命令/路径正文写进日志）。

    字段:
        field: 发生归一的字段名。
        from_type: 模型下发的原始值类型名（如 ``str``）。
        to_type: 归一后的值类型名（如 ``int``、``bool``、``list``）。
    """

    field: str
    from_type: str
    to_type: str


def coerce_tool_arguments(
    arguments: Any,
    args_model: type[BaseModel],
) -> tuple[Any, tuple[ArgumentCoercion, ...]]:
    """按 ``args_model`` 字段注解做白名单输入适配。

    参数:
        arguments: 模型下发的原始参数；非 ``Mapping``（如字符串、列表）原样返回，交由
            strict 校验给出「必须是对象」的报错。
        args_model: 工具参数模型；其字段注解是归一目标类型的唯一事实源。

    返回:
        ``(适配后的参数, 归一事实)``；未发生归一时事实为空元组，且返回值与入参内容等值
        （不修改入参对象）。

    异常:
        无。

    副作用:
        无（纯函数；不写日志，可观测由调用方落盘）。
    """

    if not isinstance(arguments, Mapping):
        return arguments, ()

    fields = args_model.model_fields
    adapted: dict[str, Any] = {}
    facts: list[ArgumentCoercion] = []
    for key, value in arguments.items():
        field = fields.get(key)
        if field is None:
            # 未知字段不归一：交给 strict 的 extra="forbid" 报错，避免拼写错误被静默丢弃。
            adapted[key] = value
            continue
        coerced, fact = _coerce_value(value, field.annotation)
        adapted[key] = coerced
        if fact is not None:
            facts.append(replace(fact, field=str(key)))
    return adapted, tuple(facts)


def _coerce_value(value: Any, annotation: Any) -> tuple[Any, ArgumentCoercion | None]:
    """按注解归一单个值；不归一时原样返回（事实为 None）。"""

    target = _resolve_target(annotation)
    if value is None or target is None:
        return value, None
    if target is bool or target is int or target is float:
        coerced = _coerce_scalar(value, target)
        if coerced is None:
            return value, None
        return coerced, _fact(value, type(coerced).__name__)
    if get_origin(target) is list:
        return _coerce_list(value, get_args(target))
    return value, None


def _coerce_scalar(value: Any, target: type) -> Any | None:
    """把标量归一为目标类型；不在白名单内返回 None（保持原值交给 strict）。"""

    if isinstance(value, bool):
        # bool 是 int 子类：只在目标为 bool 时保留，交给 strict 判定其余情况。
        return value if target is bool else None
    if isinstance(value, str):
        text = value.strip()
        if target is int and _INT_PATTERN.match(text):
            return int(text)
        if target is float and _FLOAT_PATTERN.match(text):
            return float(text)
        if target is bool:
            lowered = text.lower()
            if lowered in _TRUE_LITERALS:
                return True
            if lowered in _FALSE_LITERALS:
                return False
        return None
    if target is int and isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _coerce_list(
    value: Any,
    element_args: tuple[Any, ...],
) -> tuple[Any, ArgumentCoercion | None]:
    """把「列表元素形态」或「单值」归一为声明元素类型的列表。"""

    element_annotation = element_args[0] if len(element_args) == 1 else None
    if isinstance(value, list | tuple):
        items = list(value)
        changed = False
        for index, item in enumerate(items):
            coerced, fact = _coerce_value(item, element_annotation)
            if fact is not None:
                items[index] = coerced
                changed = True
        if changed:
            return items, _fact(value, "list")
        return value, None
    if isinstance(value, str) and not value.strip():
        # 空串/纯空白不包成单元素列表：那会构造出长度合法但内容为空的列表，
        # 从而绕过列表级约束（如 urls 的 min_length=1）——归一不得放宽任何约束。
        return value, None
    if isinstance(value, _SCALAR_SOURCES) and not isinstance(value, bool):
        # 单值 → 单元素列表：仅对可声明的标量元素生效（``urls: "https://x"`` 这类常见形态）。
        coerced, fact = _coerce_value(value, element_annotation)
        item = coerced if fact is not None else value
        return [item], _fact(value, "list")
    return value, None


def _resolve_target(annotation: Any) -> Any | None:
    """解析字段注解的目标类型；无法判定（多类型联合、未标注）时返回 None 表示不归一。"""

    annotation = _strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none) != 1:
            # ``int | str`` 之类的多类型联合存在歧义，一律不归一。
            return None
        return _resolve_target(non_none[0])
    return annotation


def _strip_annotated(annotation: Any) -> Any:
    """剥离 ``Annotated[X, ...]`` 元数据，返回基础类型。"""

    while hasattr(annotation, "__metadata__"):
        args = get_args(annotation)
        if not args:
            break
        annotation = args[0]
    return annotation


def _fact(value: Any, to_type: str) -> ArgumentCoercion:
    """构造字段级归一事实（字段名由调用方补齐）。"""

    return ArgumentCoercion(field="", from_type=type(value).__name__, to_type=to_type)
