"""工具调用的参数模式校验。

``jsonschema`` 是本模块的硬依赖，使用 Draft 2020-12 校验器对模型提供的工具参数
进行校验。校验失败时不抛出异常，而是返回人类可读的错误字符串，用于回灌为模型
观测结果，使模型能够自我纠正。
"""

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


def validate_tool_arguments(
    arguments: Any,
    schema: Mapping[str, Any],
) -> str:
    """依据 JSON Schema（Draft 2020-12）校验工具参数。

    参数:
        arguments: 模型提供的工具调用参数。
        schema: 参数模式。使用 JSON Schema（Draft 2020-12）描述的对象模式。

    返回:
        校验成功时返回空字符串，否则返回人类可读的错误。

    异常:
        无。校验失败会以文本形式返回，用于观测结果输出。

    副作用:
        无。
    """

    if not schema:
        return ""
    if not isinstance(arguments, Mapping):
        return "expected object"

    try:
        Draft202012Validator(schema).validate(dict(arguments))
    except ValidationError as exc:
        return exc.message
    return ""
