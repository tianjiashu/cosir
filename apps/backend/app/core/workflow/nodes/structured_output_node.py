"""ReAct 工作流的最终结构化输出节点。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from jsonschema import Draft202012Validator
from openai.types.chat import ChatCompletionMessage, ChatCompletionUserMessageParam
from openai.types.shared_params import ResponseFormatJSONObject

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.llm_provider.openai_model import usage_from_response
from app.models.react_execution_state import ReactExecutionState
from app.models.enums.react_route import ReactRoute
from app.core.workflow.node_helper.common import _message_context, _runtime_context, route_state
from app.service import depends as service_depends
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
    """先按 schema 判定正文，返回采纳结果或安全的重试原因。

    判定只看 ``content``：模型即使附带工具调用，只要正文已是合规 JSON 就直接采纳，不因多余调用
    丢弃可用结果。正文不合规时，重试原因由受控文案拼成（空响应、非法 JSON、不符合 schema 的路径与
    关键字），不含模型原文；若还附带了工具调用，追加「不要调用工具」并清空这批调用——支线不执行工具。

    返回:
        校验通过时返回紧凑 JSON 字符串和空原因；否则返回 ``None`` 与重试原因。

    异常:
        jsonschema.exceptions.SchemaError: schema 本身无效（正常由契约解析阶段提前拒绝）。

    副作用:
        正文不合规且带工具调用时，原地清空 ``message.tool_calls``；不改写 canonical context。
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
    run_id = runtime.run.id
    spec = runtime.agent.structured_output
    if spec is None:
        raise RuntimeError("structured_output 节点缺少输出契约")

    try:
        # load_message 返回模型请求副本（附件已解析）；重试只改动这份副本，不写回 canonical。
        context_messages = await asyncio.to_thread(_message_context().load_message)
        # 非流式 JSON 模式；本地 schema 校验最终输出契约。
        retry_feedback: str | None = None
        for attempt in range(1, Constant.Workflow.STRUCTURED_OUTPUT_MAX_ATTEMPTS + 1):
            context_messages.append(
                ChatCompletionUserMessageParam(
                    role="user",
                    content=_request_instruction(
                        spec.name, spec.json_schema, retry_feedback=retry_feedback
                    ),
                )
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
                service_depends.get_conversation_run_state_service().update_completed_result(
                    run_id,
                    final_output=final_output,
                    usage_stats=runtime.usage_stats,
                )
                # 成功即结束：循环之后的写入只属于「重试用尽或异常」，落到那里会把成功结果的
                # end_reason 覆盖成失败。
                return route_state(state.step_count, ReactRoute.END)
            log.warning(
                "structured_output_validation_failed",
                extra={
                    "msg": "结构化输出未通过校验，将按限制重试",
                    "data": {
                        "run_id": run_id,
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
                    "run_id": run_id,
                    "schema_name": spec.name,
                    "error_type": type(exc).__name__,
                },
            },
        )
    service_depends.get_conversation_run_state_service().update_completed_result(
        run_id,
        end_reason=Constant.Workflow.STRUCTURED_OUTPUT_FAILURE_REASON,
        usage_stats=runtime.usage_stats,
    )
    return route_state(state.step_count, ReactRoute.END)
