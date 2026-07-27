"""工具参数校验。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError


@dataclass(frozen=True)
class ToolArgumentValidation:
    """工具参数校验结果。

    字段:
        ok: 校验是否通过。
        arguments: 校验通过后的归一化参数字典；未通过时为空字典。
        error: 校验失败时的可读错误描述；通过时为空字符串。
    """

    ok: bool
    arguments: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def validate_tool_arguments(
    arguments: Any,
    schema: Mapping[str, Any],
    args_model: type[BaseModel] | None = None,
    required_params: tuple[str, ...] = (),
) -> ToolArgumentValidation:
    """校验模型下发的工具调用参数。

    三条路径：
    1. 传入 ``args_model`` 时，用 pydantic 模型校验并归一化为字典；此时
       ``required_params`` 不参与校验（必填字段由模型自身约束），二者互斥。
    2. 未传 ``args_model`` 且 ``schema`` 为空时，仅做「入参须为对象」与
       ``required_params`` 缺失检查。
    3. 未传 ``args_model`` 且 ``schema`` 非空时，先用 JSON Schema（Draft 2020-12）
       校验入参，再补做 ``required_params`` 缺失检查；schema 自身非法也会被
       归一化为校验失败，不会向上抛异常。

    函数契约：无论参数非法还是 schema 自身非法，始终返回 ``ToolArgumentValidation``，
    绝不抛出，便于调度层统一转为错误观察。

    参数:
        arguments: 模型下发的原始参数（通常为 dict / Mapping）。
        schema: JSON Schema 定义；为空或缺失时走非 schema 校验路径。
        args_model: 可选的 pydantic 模型；一旦传入即以前者为准，忽略 ``required_params``。
        required_params: 工具级必填参数名；仅在 ``args_model`` 为 None 时生效。

    返回:
        ok=True 时 ``arguments`` 为归一化后的参数字典；ok=False 时 ``error`` 为可读错误。

    异常:
        无（schema 自身非法也归一化为 ok=False，不向外抛出）。

    副作用:
        无（纯函数）。
    """

    if args_model is not None:
        try:
            model = args_model.model_validate(arguments)
        except PydanticValidationError as exc:
            errors = exc.errors()
            message = errors[0].get("msg") if errors else None
            return ToolArgumentValidation(ok=False, error=str(message or exc))
        return ToolArgumentValidation(ok=True, arguments=model.model_dump())

    if not schema:
        if isinstance(arguments, Mapping):
            missing = [param for param in required_params if param not in arguments]
            if missing:
                return ToolArgumentValidation(
                    ok=False, error=f"missing required argument: {missing[0]}"
                )
            return ToolArgumentValidation(ok=True, arguments=dict(arguments))
        return ToolArgumentValidation(ok=False, error="expected object")

    if not isinstance(arguments, Mapping):
        return ToolArgumentValidation(ok=False, error="expected object")

    try:
        Draft202012Validator(schema).validate(dict(arguments))
    except (JsonSchemaValidationError, SchemaError) as exc:
        return ToolArgumentValidation(ok=False, error=exc.message)
    missing = [param for param in required_params if param not in arguments]
    if missing:
        return ToolArgumentValidation(ok=False, error=f"missing required argument: {missing[0]}")
    return ToolArgumentValidation(ok=True, arguments=dict(arguments))
