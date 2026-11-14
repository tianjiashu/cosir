"""一次工具调用的原始参数 → 调用意图 + 业务参数：唯一的转换入口。

模型写下的调用参数（assistant 消息里那段 JSON）不是工具的业务参数：里面夹着每个调用都有的
``intent``，值的类型也可能要先做宽容归一。需要业务参数的地方都只走这里，拿到的与 handler 收到的
是同一份——执行门禁（准入时）与决定入口（用户作答时对照原调用校验载荷）是目前的两个调用方。各自
``json.loads`` 再拼步骤，漏一步就会出现「门禁放行、另一处拒绝」（决定入口曾因此把 ``intent`` 当成
非法字段，拒掉所有回答）。

只做展示的读取方（流式投影、冷重建、会话观测页）不走这里：它们要容错展示未完成或非法的参数，
见 ``call_intent.display_call_arguments``。
"""

from dataclasses import dataclass
from typing import Any

from app.core.llm_provider.message_codec import decode_tool_arguments
from app.core.tool.call_intent import split_call_arguments
from app.core.tool.schemas.tool_definition import ToolDefinition
from app.core.tool.validation.argument_coercion import ArgumentCoercion
from app.core.tool.validation.arguments import validate_tool_arguments


@dataclass(frozen=True)
class CallArguments:
    """``arguments`` 是按工具参数模型校验并归一后的业务参数；``coercions`` 只供调用方落日志。"""

    intent: str
    arguments: dict[str, Any]
    coercions: tuple[ArgumentCoercion, ...] = ()


class CallArgumentsError(ValueError):
    """原始参数不能成为这个工具的业务参数；消息是面向模型的完整说明（错在哪、怎么改）。"""


def resolve_call_arguments(tool: ToolDefinition, raw: str) -> CallArguments:
    """解码、拆出意图、宽容归一并按工具的参数模型 strict 校验。

    异常:
        CallArgumentsError: JSON 不合法、缺少或写错 ``intent``、业务参数不合工具声明。
    """
    try:
        intent, arguments = split_call_arguments(decode_tool_arguments(raw))
    except ValueError as exc:
        raise CallArgumentsError(
            f"Invalid tool arguments. {exc}. Supply intent and correct the arguments before retrying."
        ) from exc
    validation = validate_tool_arguments(arguments, tool.parameters_schema, tool.args_model)
    if not validation.ok:
        raise CallArgumentsError(
            f"invalid tool arguments: {validation.error}. "
            "This is deterministic, so fix the argument values/types per "
            "the tool's parameter schema and call again. The same "
            "arguments will always be rejected."
        )
    return CallArguments(intent, validation.arguments, validation.coercions)
