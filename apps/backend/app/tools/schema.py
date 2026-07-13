"""工具调用的参数模式校验。"""

from collections.abc import Mapping
from typing import Any


def validate_tool_arguments(
    arguments: Any,
    schema: Mapping[str, Any],
) -> str:
    """依据类 JSON Schema 的对象模式校验工具参数。

    参数:
        arguments: 模型提供的工具调用参数。
        schema: 参数模式。当安装了可选的 ``jsonschema`` 包时，完整的模式会委托给它；
            否则强制使用一个小范围的对象/属性/类型子集。

    返回:
        校验成功时返回空字符串，否则返回人类可读的错误。

    异常:
        无。校验失败会以文本形式返回，用于观测结果输出。

    副作用:
        如果已安装，则导入可选的 jsonschema 包。
    """

    if not schema:
        return ""
    if not isinstance(arguments, Mapping):
        return "expected object"

    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError
    except ModuleNotFoundError:
        return _validate_basic_object_schema(arguments, schema)

    try:
        Draft202012Validator(schema).validate(dict(arguments))
    except ValidationError as exc:
        return exc.message
    return ""


def _validate_basic_object_schema(
    arguments: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> str:
    """使用内置的模式子集校验参数。

    参数:
        arguments: 模型提供的工具调用参数。
        schema: 使用 ``properties``、``required``、``additionalProperties`` 与
            基础 ``type`` 声明的、类 JSON Schema 的对象模式。

    返回:
        校验成功时返回空字符串，否则返回人类可读的错误。

    异常:
        无。

    副作用:
        无。
    """

    if schema.get("type", "object") != "object":
        return "tool parameter schema must be an object schema"

    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return "tool parameter schema properties must be an object"

    for name in schema.get("required", ()):
        if name not in arguments:
            return f"missing required parameter: {name}"

    if schema.get("additionalProperties", True) is False:
        extra = sorted(name for name in arguments if name not in properties)
        if extra:
            return f"unexpected parameter: {extra[0]}"

    for name, value in arguments.items():
        property_schema = properties.get(name)
        if not isinstance(property_schema, Mapping):
            continue
        expected_type = property_schema.get("type")
        if expected_type and not _matches_json_type(value, expected_type):
            return f"{name}: expected {expected_type}"

    return ""


def _matches_json_type(value: Any, expected_type: Any) -> bool:
    """返回 Python 值是否匹配 JSON Schema 的基础类型。

    参数:
        value: 待检查的 Python 值。
        expected_type: JSON Schema 类型名或类型名列表。

    返回:
        当值匹配任一期望的基础类型时为 True。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(expected_type, list):
        return any(_matches_json_type(value, item) for item in expected_type)

    type_checks = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "object": lambda item: isinstance(item, Mapping),
        "array": lambda item: isinstance(item, list),
        "null": lambda item: item is None,
    }
    check = type_checks.get(expected_type)
    if check is None:
        return True
    return check(value)
