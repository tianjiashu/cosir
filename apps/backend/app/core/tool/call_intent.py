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
    # 每个工具都带这个字段，完整写法只在系统提示词工具层写一次（``SystemPromptBuilder`` 的
    # intent 规则）；这里只留一句指过去，否则同一段说明随每个工具重复发出几十遍。
    "description": "What this call does and why, for the user; follow the intent rule in your instructions.",
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


def parse_call_intent(value: object) -> str | None:
    """非空短字符串收紧空白后返回，缺失或非法为 ``None``；执行与展示共用这一条规则。

    schema 把 intent 声明为必填是对模型的要求，执行边界却不因它拒绝调用：意图只服务展示，执行、
    授权与日志都不读它，而不强制 strict 的厂商（如 DeepSeek 非 beta 地址）在长参数调用里会漏写它，
    拒绝只换来模型重新生成整份参数。缺失时展示退回工具动词，后台标签由各自登记方回退。
    """
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_INTENT_LENGTH:
        return None
    return value.strip()


def split_call_arguments(arguments: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """执行边界拆出意图；业务校验和 Hook 只接收剩余参数，不修改原始调用。"""
    business = dict(arguments)
    return parse_call_intent(business.pop("intent", None)), business


def display_call_arguments(raw: str) -> tuple[str | None, dict[str, Any]]:
    """实时收口和冷重建的同一投影；非法或残缺意图不猜测、不影响诊断参数。"""
    arguments = display_arguments(raw)
    return parse_call_intent(arguments.pop("intent", None)), arguments
