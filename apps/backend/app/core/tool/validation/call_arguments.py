"""一次工具调用的原始参数 → 业务参数：唯一的转换入口。

模型写下的调用参数（assistant 消息里那段 JSON）要先解码，值的类型也可能要先做宽容归一。需要业务参数的
地方都只走这里，拿到的与 handler 收到的是同一份——执行门禁（准入时）与决定入口（用户作答时对照原调用
校验载荷）是目前的两个调用方。延迟工具（``resolve_external_arguments``）同一套解码与错误文案，只是校验
依据换成来源给的 JSON Schema。各自 ``json.loads`` 再拼步骤，漏一步就会出现「门禁放行、另一处拒绝」。

只做展示的读取方（流式投影、冷重建、会话观测页）不走这里：它们要容错展示未完成或非法的参数，
见 ``message_codec.display_arguments``。
"""

from dataclasses import dataclass
from typing import Any

from app.core.llm_provider.message_codec import decode_tool_arguments
from app.core.tool.schemas.tool_definition import ToolDefinition
from app.core.tool.validation.argument_coercion import ArgumentCoercion
from app.core.tool.validation.arguments import ToolArgumentValidation, validate_tool_arguments
from app.models.external_tool import ExternalToolDefinition


@dataclass(frozen=True)
class CallArguments:
    """``arguments`` 是按工具参数模型校验并归一后的业务参数；``coercions`` 只供调用方落日志。"""

    arguments: dict[str, Any]
    coercions: tuple[ArgumentCoercion, ...] = ()


class CallArgumentsError(ValueError):
    """原始参数不能成为这个工具的业务参数；消息是面向模型的完整说明（错在哪、怎么改）。"""


def resolve_call_arguments(tool: ToolDefinition, raw: str) -> CallArguments:
    """解码、宽容归一并按工具的参数模型 strict 校验。

    异常:
        CallArgumentsError: JSON 不合法、业务参数不合工具声明。
    """
    validation = validate_tool_arguments(_decode(raw), tool.parameters_schema, tool.args_model)
    return CallArguments(_checked(validation).arguments, validation.coercions)


def resolve_external_arguments(definition: ExternalToolDefinition, raw: str) -> CallArguments:
    """延迟工具：解码，再按来源给的 JSON Schema 校验整包参数。

    不做宽容归一：归一规则按 pydantic 参数模型的声明类型走，外部 schema 没有这份声明。

    异常:
        CallArgumentsError: JSON 不合法、参数不合 schema，或 schema 自身非法（说明文字写明是哪一种）。
    """
    validation = validate_tool_arguments(_decode(raw), definition.parameters)
    return CallArguments(_checked(validation).arguments)


def _decode(raw: str) -> dict[str, Any]:
    try:
        return decode_tool_arguments(raw)
    except ValueError as exc:
        raise CallArgumentsError(
            f"Invalid tool arguments. {exc}. Correct the arguments before retrying."
        ) from exc


def _checked(validation: ToolArgumentValidation) -> ToolArgumentValidation:
    if not validation.ok:
        raise CallArgumentsError(
            f"invalid tool arguments: {validation.error}. "
            "This is deterministic, so fix the argument values/types per "
            "the tool's parameter schema and call again. The same "
            "arguments will always be rejected."
        )
    return validation
