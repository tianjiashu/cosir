"""ReAct 工作流的最终结构化输出节点。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from jsonschema import Draft202012Validator
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflows.react.node_helper.common import (
    _runtime_config,
    _runtime_context,
    route_state,
)
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.core.workflows.vision_input import resolve_messages_for_model
from app.models.conversation_run_failure import run_failure_message
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
        可作为临时 HumanMessage 内容的指令字符串。

    异常:
        TypeError: 输入中含不能 JSON 序列化的 schema 值。

    副作用:
        无；不会写入 RuntimeContextManager。
    """

    sections = [
        "Produce the final structured output now. Do not continue the analysis or call tools.",
        "Based on the full conversation above, generate a JSON value that conforms to the specified JSON Schema.",
        "Return only JSON. Do not include Markdown fences, explanations, or tool calls.",
        f"Schema name: {schema_name}",
        "JSON Schema:",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "Do not call tools. Use prior tool interactions in the conversation as context only.",
    ]
    if retry_feedback is not None:
        sections.extend(
            [
                "The previous response did not pass JSON or schema validation. Generate a complete JSON value again and fix the following issue:",
                retry_feedback,
            ]
        )
    return "\n\n".join(sections)


def _reject_json_constant(value: str) -> None:
    """拒绝 Python JSON 解码器额外接受的 NaN 与 Infinity 常量。"""

    raise ValueError(f"非标准 JSON 常量：{value}")


def _validate_response(message: BaseMessage, schema: dict[str, Any]) -> tuple[str | None, str]:
    """拒绝工具调用、非 JSON 内容和不满足 schema 的 JSON，返回安全的重试原因。

    参数:
        message: 模型单次非流式响应。
        schema: 输出契约中的 JSON Schema。

    返回:
        校验通过时返回紧凑 JSON 字符串和空原因；否则返回 ``None`` 与不含模型原文的原因。

    异常:
        jsonschema.exceptions.SchemaError: 调用方传入的 schema 本身无效（正常由契约解析阶段
            提前拒绝）。

    副作用:
        无。
    """
    ai_message = cast(AIMessage, message)
    ai_message.tool_calls = []
    ai_message.invalid_tool_calls = []
    text = content_to_text(message.content).strip()
    if not text:
        return None, "The response is empty."
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, "The response is not valid JSON."

    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error is not None:
        escaped_path = (
            str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path
        )
        path = "/" + "/".join(escaped_path)
        return None, f"The JSON does not conform to the schema (path: {path}; keyword: {error.validator})."
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")), ""


async def _structured_output_node(state: ReactGraphState) -> dict:
    """以完整 context 生成并校验最终 JSON，成功时把结果改写进 Run.final_output。

    参数:
        state: 当前 graph state；只读取模型步骤号，不把结构化结果写入 state。

    返回:
        指向 ``END`` 的 LangGraph state 增量。本节点**不迁移 Run 状态**：完成态已由 ``model``
        节点落定，校验通过的结果只经 ``fix_final_if_complete`` 改写 ``final_output``；JSON 内容
        不进入 graph state、RuntimeContextManager 或对话消息。

    异常:
        模型调用、图片解析或持久化异常向上抛出，由 ``AgentRuntime.run_agent``
        的异常边界记录。但此时 Run 已由 ``model`` 节点落为 ``completed``，
        ``fail_run_if_running`` 的条件更新会落空：终态与已写入的自然语言
        回复都不会被改写。

    副作用:
        最多发起四次非流式模型请求（初次调用加三次重试）；用量写入 Run 级统计。
        校验通过时调用 ``WorkflowOperations.fix_final_if_complete``：仅当 Run
        仍为 ``completed`` 才把 ``final_output`` 改写为结构化 JSON，不改
        状态、不发状态事件。四次输出均无效时不落定任何状态——Run 保持
        ``completed`` 与 ``model`` 节点写入的自然语言回复，只记 error 日志；
        检测到取消同样直接结束本节点。请求使用 context 副本；每轮把临时指令
        和模型响应追加到副本，使后续重试可以参考此前失败的尝试，不修改
        canonical context。
    """

    runtime = _runtime_config()
    operations = runtime.operations
    spec = runtime.structured_output
    if spec is None:
        raise RuntimeError("structured_output 节点缺少输出契约")

    task = operations.get_current_task()
    context_messages = _runtime_context().load_message()
    # 先解析历史消息中的图片引用；后续重试只改动此副本，不写回 canonical context。
    context_messages = await asyncio.to_thread(
        resolve_messages_for_model,
        context_messages,
        workspace_id=task.workspace_id,
    )
    # 复用 ReAct 的工具绑定模型并请求供应商严格 JSON Schema 输出；本地校验仍负责
    # 防御供应商不遵守契约的情况，同时保持请求中的工具 schema 不变。
    # DeepSeek（base_url=https://api.deepseek.com，model deepseek-flash）
    # 只支持 response_format={"type":"json_object"}，不接受 json_schema
    model = runtime.model.bind(
        response_format={"type": "json_object"}
    )
    retry_feedback: str | None = None
    for attempt in range(1, Constant.Workflow.STRUCTURED_OUTPUT_MAX_ATTEMPTS + 1):
        if operations.is_current_run_cancelled():
            return route_state(state.step_count, ReactRoute.END)

        instruction = _request_instruction(
            spec.name,
            spec.json_schema,
            retry_feedback=retry_feedback,
        )
        # 在上下文副本末尾追加本轮指令；前序失败响应保留在副本中供模型修正。
        context_messages.append(HumanMessage(content=instruction))
        response = await model.ainvoke(context_messages)
        runtime.usage_stats.add_usage_metadata(response.usage_metadata)

        final_output, retry_feedback = _validate_response(response, spec.json_schema)
        if final_output is not None:
            operations.fix_final_if_complete(
                runtime.usage_stats,
                final_output=final_output,
            )
            return route_state(state.step_count, ReactRoute.END)
        context_messages.append(response)
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

    log.error(
        "structured_output_exhausted",
        extra={
            "msg": "结构化输出连续校验失败，保留自然语言最终回复，Run 状态不变",
            "data": {
                "run_id": runtime.run.id,
                "schema_name": spec.name,
                "attempts": Constant.Workflow.STRUCTURED_OUTPUT_MAX_ATTEMPTS,
            },
        },
    )
    return route_state(state.step_count, ReactRoute.END)
