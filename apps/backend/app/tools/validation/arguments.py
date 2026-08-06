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
) -> ToolArgumentValidation:
    """校验模型下发的工具调用参数。

    三条路径：
    1. 传入 ``args_model`` 时，用 pydantic 模型校验并归一化为字典（必填字段由
       模型自身约束）。
    2. 未传 ``args_model`` 且 ``schema`` 为空时，仅做「入参须为对象」检查。
    3. 未传 ``args_model`` 且 ``schema`` 非空时，用 JSON Schema（Draft 2020-12）
       校验入参；schema 自身非法也会被归一化为校验失败，不会向上抛异常。

    函数契约：无论参数非法还是 schema 自身非法，始终返回 ``ToolArgumentValidation``，
    绝不抛出，便于调度层统一转为错误观察。失败时的 ``error`` 为面向模型（Agent）的
    聚合可读描述：包含出错字段/位置与原因，便于模型一次修正多处，而非仅报告首个错误。

    参数:
        arguments: 模型下发的原始参数（通常为 dict / Mapping）。
        schema: JSON Schema 定义；为空或缺失时走非 schema 校验路径。
        args_model: 可选的 pydantic 模型；一旦传入即以前者为准校验并归一化入参。

    返回:
        ok=True 时 ``arguments`` 为归一化后的参数字典；ok=False 时 ``error`` 为聚合后的
        可读错误（含出错字段/位置与原因）。

    异常:
        无（schema 自身非法也归一化为 ok=False，不向外抛出）。

    副作用:
        无（纯函数）。
    """

    if args_model is not None:
        try:
            model = args_model.model_validate(arguments)
        except PydanticValidationError as exc:
            parts = []
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
                parts.append(f"{loc}: {err.get('msg', 'invalid value')}")
            detail = "; ".join(parts)
            return ToolArgumentValidation(
                ok=False, error=f"Argument validation failed ({len(parts)} issue(s)): {detail}"
            )
        return ToolArgumentValidation(ok=True, arguments=model.model_dump())

    if not schema:
        if isinstance(arguments, Mapping):
            return ToolArgumentValidation(ok=True, arguments=dict(arguments))
        return ToolArgumentValidation(
            ok=False,
            error=(
                f"Arguments must be an object (key-value structure), "
                f"but received {type(arguments).__name__}"
            ),
        )

    if not isinstance(arguments, Mapping):
        return ToolArgumentValidation(
            ok=False,
            error=(
                f"Arguments must be an object (key-value structure), "
                f"but received {type(arguments).__name__}"
            ),
        )

    try:
        Draft202012Validator(schema).validate(dict(arguments))
    except SchemaError as exc:
        return ToolArgumentValidation(
            ok=False,
            error=(f"Tool argument schema definition is invalid: {exc.message}"),
        )
    except JsonSchemaValidationError as exc:
        path = getattr(exc, "json_path", "") or ""
        where = f" (at {path})" if path else ""
        return ToolArgumentValidation(
            ok=False,
            error=(f"Arguments do not match schema{where}: {exc.message}"),
        )
    return ToolArgumentValidation(ok=True, arguments=dict(arguments))
