"""ReAct 工作流的最终结构化输出节点。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from jsonschema import Draft202012Validator
from openai.types.chat import ChatCompletionMessage
from openai.types.shared_params import ResponseFormatJSONObject

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.llm_provider.openai_model import usage_from_response
from app.core.workflow.execution_state.route import ReactRoute
from app.core.workflow.execution_state.state import ReactExecutionState
from app.core.workflow.node_helper.common import _message_context, _runtime_context, route_state
from app.core.workflow.vision_input import resolve_messages_for_model
from app.utils.message_content import content_to_text


def _request_instruction(
    schema_name: str,
    schema: dict[str, Any],
    *,
    retry_feedback: str | None,
) -> str:
    """构造本次请求末尾的临时指令，不改写或重排 canonical context。

    参数:
        schema_name: 输出契约名称。
        schema: 已通过 Draft 2020-12 校验的 JSON Schema。
        retry_feedback: 上一轮的受控校验失败原因；首轮为 ``None``。

    返回:
        可作为临时 user 消息 内容的指令字符串。

    异常:
        TypeError: 输入中含不能 JSON 序列化的 schema 值。

    副作用:
        无；不会写入 RuntimeMessageContextManager。
    """
    sections = []
    if retry_feedback is not None:
        sections.extend(
            [
                (
                    "The previous response did not pass JSON or schema validation. "
                    "Generate a complete JSON value again and fix the following issue:"
                ),
                retry_feedback,
            ]
        )
    else:
        sections.extend(
            [
                "Produce the final structured output now. Do not continue the analysis or call tools.",
                (
                    "Based on the full conversation above, generate a JSON value "
                    "that conforms to the specified JSON Schema."
                ),
                "Return only JSON. Do not include Markdown fences, explanations, or tool calls.",
                f"Schema name: {schema_name}",
                "JSON Schema:",
                json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                "Do not call tools. Use prior tool interactions in the conversation as context only.",
            ]
        )
    return "\n\n".join(sections)


def _reject_json_constant(value: str) -> None:
    """拒绝 Python JSON 解码器额外接受的 NaN 与 Infinity 常量。"""

    raise ValueError(f"非标准 JSON 常量：{value}")


def _validate_response(
    message: ChatCompletionMessage,
    schema: dict[str, Any],
) -> tuple[str | None, str]:
    """先按 schema 判定正文，再处理被拒绝的那一轮，返回安全的重试原因。

    判定只看 ``content``：模型即使附带工具调用，只要正文已是合规 JSON 就直接采纳，
    不因多余调用丢弃可用结果。正文不合规时才检查 ``tool_calls``，把本步不允许的工具
    调用以「失败配对」写回请求副本（见 :func:`_append_rejected_turn`），并用受控文案
    说明原因，避免模型原文或工具输出进入提示词。

    参数:
        message: 模型单次非流式响应。
        schema: 输出契约中的 JSON Schema。
        request_messages: 本次支线的临时请求副本（不是 canonical context）；被拒绝的轮次
            追加到其末尾，供下一轮重试读到。

    返回:
        校验通过时返回紧凑 JSON 字符串和空原因；否则返回 ``None`` 与不含模型原文的原因。

    异常:
        jsonschema.exceptions.SchemaError: 调用方传入的 schema 本身无效（正常由契约解析阶段
            提前拒绝）。

    副作用:
        校验失败时原地追加 ``request_messages``；不执行工具，不改写 canonical context。
    """

    retry_reasons: list[str] = []
    text = content_to_text(message.content).strip()
    if not text:
        retry_reasons.append("The response is empty.")
    else:
        try:
            value = json.loads(text, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, TypeError, ValueError):
            retry_reasons.append("The response is not valid JSON.")
        else:
            error = next(Draft202012Validator(schema).iter_errors(value), None)
            if error is None:
                return json.dumps(value, ensure_ascii=False, separators=(",", ":")), ""
            escaped_path = (
                str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path
            )
            path = "/" + "/".join(escaped_path)
            retry_reasons.append(
                "The JSON does not conform to the schema "
                f"(path: {path}; keyword: {error.validator})."
            )

    if message.tool_calls:
        retry_reasons.append("don't call tool")
        message.tool_calls = []
    return None, " ".join(retry_reasons)


async def _structured_output_node(state: ReactExecutionState) -> dict:
    """已完成 Run 的结果支线：成功替换 final_output，失败只记录 end_reason。

    请求使用主线完成前冻结的 context 副本，不消费 inbox；支线不迁移 Run 状态。
    """

    runtime = _runtime_context()
    operations = runtime.operations
    spec = runtime.structured_output
    if spec is None:
        raise RuntimeError("structured_output 节点缺少输出契约")

    messages = _message_context().load_message()

    request_failed = False
    try:
        task = operations.get_current_task()
        # 图片解析和重试只改动冻结副本，不写回 canonical context。
        context_messages = await asyncio.to_thread(
            resolve_messages_for_model,
            messages,
            workspace_id=task.workspace_id,
        )
        # 非流式 JSON 模式；本地 schema 校验最终输出契约。
        retry_feedback: str | None = None
        final_output: str | None = None
        for attempt in range(1, Constant.Workflow.STRUCTURED_OUTPUT_MAX_ATTEMPTS + 1):
            context_messages.append(
                {
                    "role": "user",
                    "content": _request_instruction(
                        spec.name, spec.json_schema, retry_feedback=retry_feedback
                    ),
                }
            )
            response = await runtime.model.complete(
                context_messages, response_format=ResponseFormatJSONObject(type="json_object")
            )
            runtime.usage_stats.add_usage_metadata(usage_from_response(response.usage))
            choice = response.choices[0]
            final_output, retry_feedback = _validate_response(
                choice.message, spec.json_schema
            )
            if final_output is not None:
                operations.update_completed_result(
                    final_output=final_output,
                    usage_stats=runtime.usage_stats,
                )
                break
            log.warning(
                "structured_output_validation_failed",
                extra={
                    "msg": "结构化输出未通过校验，将按限制重试",
                    "data": {
                        "run_id": runtime.run.id,
                        "schema_name": spec.name,
                        "attempt": attempt,
                        "reason": retry_feedback,
                    },
                },
            )
    except Exception as exc:
        log.exception(
            "structured_output_request_failed",
            extra={
                "msg": "结构化输出支线异常，保留自然语言最终回复",
                "data": {
                    "run_id": runtime.run.id,
                    "schema_name": spec.name,
                    "error_type": type(exc).__name__,
                },
            },
        )
    operations.update_completed_result(
        end_reason="结构化输出支线异常，保留自然语言最终回复",
        usage_stats=runtime.usage_stats,
    )
    return route_state(state.step_count, ReactRoute.END)
