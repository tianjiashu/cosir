"""wire 契约的运行时校验原语。

字段集合只在 TypedDict 声明一处维护：校验层用 :func:`as_shape` 按声明收敛形状，不另写键集合常量，
也不把校验过的对象继续当 ``dict[str, object]`` 使用。取值守卫的参数类型是 ``object``：它们要挡的是
运行期真实类型与声明不符的数据（例如模型 HTTP 响应体直接提供的文案），与静态声明不冲突，也不会让
类型检查把必要守卫判成不可达分支。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import cache
from typing import Any, TypeVar, cast

_Shape = TypeVar("_Shape", bound=Mapping[str, object])


@cache
def _declared_fields(shape: Any) -> tuple[frozenset[str], frozenset[str]]:
    """返回 ``(必填字段, 全部声明字段)``；TypedDict 的字段集合在运行期可读。"""

    required = frozenset(shape.__required_keys__)
    return required, required | frozenset(shape.__optional_keys__)


def as_shape(value: object, shape: type[_Shape], label: str) -> _Shape:
    """按 ``shape`` 的声明校验 ``value`` 的字段集合并返回类型化视图。

    必填字段必须齐全，``NotRequired`` 字段可省略，未声明的字段一律拒绝。``shape`` 所在的模块不能
    用 ``from __future__ import annotations``：注解被字符串化后，运行期读不到 ``NotRequired``，
    可选字段会被当成必填。

    异常:
        ValueError: ``value`` 不是对象，或字段集合不符合契约。
    """

    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    required, declared = _declared_fields(shape)
    keys = set(value)
    if not required <= keys or not keys <= declared:
        raise ValueError(
            f"{label} fields are invalid"
            f" (missing={sorted(required - keys)}, unknown={sorted(keys - declared)})"
        )
    return cast(_Shape, value)


def require_str(value: object, label: str, *, nullable: bool = False) -> None:
    """要求 ``value`` 是字符串；``nullable`` 时允许 ``None``。"""

    if value is None and nullable:
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string" + (" or null" if nullable else ""))


def require_non_empty_str(value: object, label: str) -> str:
    """要求 ``value`` 是非空白字符串，并返回它。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def require_bool(value: object, label: str) -> None:
    """要求 ``value`` 是布尔值。"""

    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")


def require_int(
    value: object, label: str, *, minimum: int | None = None, nullable: bool = False
) -> None:
    """要求 ``value`` 是整数（``bool`` 不算）；``minimum`` 限定下界，``nullable`` 允许 ``None``。"""

    if value is None and nullable:
        return
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or (minimum is not None and value < minimum)
    ):
        expected = "an integer" if minimum is None else f"an integer >= {minimum}"
        raise ValueError(f"{label} must be {expected}" + (" or null" if nullable else ""))


def require_mapping(value: object, label: str, *, nullable: bool = False) -> None:
    """要求 ``value`` 是对象；``nullable`` 时允许 ``None``。"""

    if value is None and nullable:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object" + (" or null" if nullable else ""))


def require_list(value: object, label: str) -> list[object]:
    """要求 ``value`` 是数组，并返回它。"""

    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def require_str_list(value: object, label: str) -> None:
    """要求 ``value`` 是字符串数组。"""

    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be an array of strings")


def require_one_of(value: object, allowed: frozenset[str], label: str) -> None:
    """要求 ``value`` 落在 ``allowed`` 之内。"""

    if value not in allowed:
        raise ValueError(f"{label} is invalid")


def require_locator(value: object, pattern: re.Pattern[str], label: str) -> None:
    """要求 ``value`` 匹配资源定位符 ``pattern``。"""

    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{label} is invalid")
