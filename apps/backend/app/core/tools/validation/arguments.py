"""工具参数校验。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# jsonschema 4.x 的 py.typed 为 partial，类型不可靠；已由 mypy 全局
# ignore_missing_imports（见 pyproject.toml [tool.mypy] 与 mypy.strict.ini）覆盖，
# 无需局部 ignore。
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.core.tools.validation.argument_coercion import (
    ArgumentCoercion,
    coerce_tool_arguments,
)

# strict 下的类型类错误（模型把数字/布尔写成了字符串等）前缀；命中时追加可操作提示。
_TYPE_ERROR_PREFIXES = ("int_", "float_", "bool_")
_TYPE_HINT = (
    " Hint: numeric and boolean parameters must be JSON numbers/booleans "
    '(30, true), not strings ("30", "true").'
)


@dataclass(frozen=True)
class ToolArgumentValidation:
    """工具参数校验结果。

    字段:
        ok: 校验是否通过。
        arguments: 校验通过后的归一化参数字典；未通过时为空字典。
        error: 校验失败时的可读错误描述；通过时为空字符串。
        coercions: 本次校验**之前**发生的宽容归一事实（strict 白名单内，见
            ``argument_coercion``）；无归一时为空元组。仅供可观测落盘，不参与判定——
            归一后仍以 strict 校验结果为准。
    """

    ok: bool
    arguments: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    coercions: tuple[ArgumentCoercion, ...] = ()


def validate_tool_arguments(
    arguments: Any,
    schema: Mapping[str, Any],
    args_model: type[BaseModel] | None = None,
) -> ToolArgumentValidation:
    """校验模型下发的工具调用参数。

    三条路径：
    1. 传入 ``args_model`` 时，**先按白名单做输入形态适配**（``coerce_tool_arguments``，
       只还原 ``"30"``→30 这类无损形态），再用 pydantic **strict** 模型校验并归一化为
       字典；适配不改变任何校验规则，必填、范围、枚举与 ``extra="forbid"`` 仍由模型约束。
    2. 未传 ``args_model`` 且 ``schema`` 为空时，仅做「入参须为对象」检查。
    3. 未传 ``args_model`` 且 ``schema`` 非空时，用 JSON Schema（Draft 2020-12）
       校验入参；schema 自身非法也会被归一化为校验失败，不会向上抛异常。

    函数契约：无论参数非法还是 schema 自身非法，始终返回 ``ToolArgumentValidation``，
    绝不抛出，便于调度层统一转为错误观察。失败时的 ``error`` 为面向模型（Agent）的
    聚合可读描述：包含出错字段/位置与原因，便于模型一次修正多处，而非仅报告首个错误；
    命中类型类错误时追加一句「数字/布尔须传 JSON 原生类型」的可操作提示。
    通过时 ``coercions`` 记录本次发生过的输入适配事实，供调用方落日志。

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
        # 只适配输入形态（白名单内、无损），随后仍是 strict 校验唯一判定。
        adapted, coercions = coerce_tool_arguments(arguments, args_model)
        try:
            model = args_model.model_validate(adapted)
        except PydanticValidationError as exc:
            parts = []
            type_mismatch = False
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
                parts.append(f"{loc}: {err.get('msg', 'invalid value')}")
                if str(err.get("type", "")).startswith(_TYPE_ERROR_PREFIXES):
                    type_mismatch = True
            detail = "; ".join(parts)
            hint = _TYPE_HINT if type_mismatch else ""
            return ToolArgumentValidation(
                ok=False,
                error=f"Argument validation failed ({len(parts)} issue(s)): {detail}{hint}",
            )
        return ToolArgumentValidation(
            ok=True,
            arguments=model.model_dump(),
            coercions=coercions,
        )

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
