"""模型调用意图的公共边界；不属于任何工具的业务参数或执行承诺。"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from app.core.llm_provider.message_codec import display_arguments

MAX_INTENT_LENGTH = 160
_INTENT_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_INTENT_LENGTH,
    "description": (
        "A short sentence in the user's language describing the purpose of this call. "
        "Describe what you intend to do and why; do not claim progress, success or a result."
    ),
}


def model_parameters_with_intent(schema: Mapping[str, Any]) -> dict[str, Any]:
    """在模型出口注入保留字段，保留原业务 schema（含引用）；冲突是装配错误。"""
    result = deepcopy(dict(schema))
    properties = result.get("properties", {})
    if "intent" in properties:
        raise ValueError("tool business schema uses reserved field: intent")
    result["properties"] = {"intent": deepcopy(_INTENT_SCHEMA), **properties}
    result["required"] = ["intent", *result.get("required", [])]
    return result


def validate_call_intent(value: object) -> str:
    """只接受非空短字符串；模型输出与持久化展示在各自输入边界复用本契约。"""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_INTENT_LENGTH:
        raise ValueError(
            f"intent must be a nonempty string of at most {MAX_INTENT_LENGTH} characters"
        )
    return value.strip()


def split_call_arguments(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """执行边界拆出意图；业务校验和 Hook 只接收剩余参数，不修改原始调用。"""
    business = dict(arguments)
    intent = validate_call_intent(business.pop("intent", None))
    return intent, business


def display_call_arguments(raw: str) -> tuple[str | None, dict[str, Any]]:
    """实时收口和冷重建的同一投影；非法或残缺意图不猜测、不影响诊断参数。"""
    arguments = display_arguments(raw)
    value = arguments.pop("intent", None)
    try:
        intent = validate_call_intent(value)
    except ValueError:
        intent = None
    return intent, arguments
