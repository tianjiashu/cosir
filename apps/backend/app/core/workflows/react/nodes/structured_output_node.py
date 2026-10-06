"""ReAct 工作流的最终结构化输出节点。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from jsonschema import Draft202012Validator
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflows.react.node_helper.common import (
    _runtime_config,
    _runtime_context,
    end_state,
)
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.core.workflows.vision_input import resolve_messages_for_model
from app.models.conversation_run_failure import run_failure_message
from app.utils.message_content import content_to_text

_MAX_ATTEMPTS = 4


def _request_instruction(
    schema_name: str,
    schema: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
    *,
    retry_feedback: str | None,
) -> str:
    """构造本次请求末尾的临时指令，不改写或重排 canonical context。

    参数:
        schema_name: 输出契约名称。
        schema: 已通过 Draft 2020-12 校验的 JSON Schema。
        tool_schemas: 当前 Task 冻结的工具 schema，仅作为上下文说明。
        retry_feedback: 上一轮的受控校验失败原因；首轮为 ``None``。

    返回:
        可作为临时 HumanMessage 内容的指令字符串。

    异常:
        TypeError: 输入中含不能 JSON 序列化的 schema 值。

    副作用:
        无；不会写入 RuntimeContextManager。
    """

    sections = [
        "现在只执行最终结构化输出，不再继续分析或调用工具。",
        "请根据上方完整对话上下文生成一个符合指定 JSON Schema 的 JSON 值。",
        "只能返回 JSON，不得返回 Markdown 代码围栏、解释文字或工具调用。",
        f"Schema 名称：{schema_name}",
        "JSON Schema：",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "本任务可用工具 schema（仅供理解此前工具调用，不得调用工具）：",
        json.dumps(tool_schemas, ensure_ascii=False, separators=(",", ":")),
    ]
    if retry_feedback is not None:
        sections.extend(
            [
                "上一次结果未通过格式或 schema 校验。请重新生成完整 JSON，修正以下问题：",
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

    if not isinstance(message, AIMessage):
        return None, "模型响应不是 assistant 消息。"
    if (
        message.tool_calls
        or message.invalid_tool_calls
        or message.additional_kwargs.get("tool_calls")
        or message.additional_kwargs.get("function_call")
    ):
        return None, "响应包含工具调用；本阶段禁止调用工具。"
    text = content_to_text(message.content).strip()
    if not text:
        return None, "响应内容为空。"
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, "响应不是有效 JSON。"

    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error is not None:
        escaped_path = (
            str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path
        )
        path = "/" + "/".join(escaped_path)
        return None, f"JSON 不符合 schema（路径：{path}；规则：{error.validator}）。"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")), ""


async def _structured_output_node(state: ReactGraphState) -> dict:
    """以完整 context 生成并校验最终 JSON，只把成功结果写入 Run.final_output。

    参数:
        state: 当前 graph state；只读取模型步骤号，不把结构化结果写入 state。

    返回:
        LangGraph state 增量。成功时标记最终响应；失败或取消时标记终止。JSON 内容不进入
        graph state、RuntimeContextManager 或对话消息。

    异常:
        模型调用、图片解析或持久化异常向上抛出，由工作流统一落定失败 Run。

    副作用:
        最多发起四次非流式模型请求（初次调用加三次重试）；用量写入 Run 级统计；成功时条件
        更新 Run 为 completed，四次输出均无效时条件更新为 failed。每次请求都在 context 副本
        末尾追加临时指令，不修改 canonical context，保持原有缓存前缀不变。
    """

    runtime = _runtime_config()
    operations = runtime.operations
    spec = runtime.structured_output
    if spec is None:
        raise RuntimeError("structured_output 节点缺少输出契约")

    task = operations.get_current_task()
    context_messages = _runtime_context().load_message()
    # 先解析历史消息中的图片引用，再在尾部追加请求，确保追加指令前的 context 完全不变。
    context_messages = await asyncio.to_thread(
        resolve_messages_for_model,
        context_messages,
        workspace_id=task.workspace_id,
    )
    tool_schemas = operations.task_tool_schemas
    # 使用未绑定工具的基础模型并绑定 response_format；校验仍拒绝任何意外工具调用。
    model = runtime.final_model.bind(response_format=spec.response_format())
    retry_feedback: str | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if operations.is_current_run_cancelled():
            operations.cancel_run_if_running(
                usage_stats=runtime.usage_stats,
                final_output="user_cancelled",
            )
            return end_state(state.step_count)

        instruction = _request_instruction(
            spec.name,
            spec.json_schema,
            tool_schemas,
            retry_feedback=retry_feedback,
        )
        # 每次调用共享同一份只读前缀；结构化指令仅作为本次请求的最后一条临时消息。
        request_messages = [*context_messages, HumanMessage(content=instruction)]
        response = await model.ainvoke(request_messages)
        if isinstance(response, AIMessage):
            runtime.usage_stats.add_usage_metadata(response.usage_metadata)

        if operations.is_current_run_cancelled():
            operations.cancel_run_if_running(
                usage_stats=runtime.usage_stats,
                final_output="user_cancelled",
            )
            return end_state(state.step_count)

        final_output, retry_feedback = _validate_response(response, spec.json_schema)
        if final_output is not None:
            completed_run = operations.complete_run_if_running(
                runtime.usage_stats,
                final_output=final_output,
            )
            if completed_run is None:
                log.info(
                    "structured_output_completion_race_lost",
                    extra={
                        "msg": "结构化输出完成时 Run 已不再运行，跳过重复终态写入",
                        "data": {"run_id": runtime.run.id, "attempt": attempt},
                    },
                )
                return end_state(state.step_count)
            log.info(
                "structured_output_completed",
                extra={
                    "msg": "结构化输出通过 JSON Schema 校验并写入 Run 最终结果",
                    "data": {
                        "run_id": runtime.run.id,
                        "schema_name": spec.name,
                        "attempt": attempt,
                        "output_length": len(final_output),
                    },
                },
            )
            return end_state(state.step_count)

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

    end_reason = Constant.Run.RUN_FAILURE_CODE_MODEL_OUTPUT_INVALID
    failed_run = operations.fail_run_if_running(
        end_reason=end_reason,
        usage_stats=runtime.usage_stats,
        final_output=run_failure_message(end_reason),
    )
    if failed_run is None:
        log.info(
            "structured_output_failure_race_lost",
            extra={
                "msg": "结构化输出重试耗尽时 Run 已不再运行，跳过重复失败迁移",
                "data": {"run_id": runtime.run.id, "schema_name": spec.name},
            },
        )
    else:
        log.error(
            "structured_output_exhausted",
            extra={
                "msg": "结构化输出连续校验失败，Run 已标记为失败",
                "data": {
                    "run_id": runtime.run.id,
                    "schema_name": spec.name,
                    "attempts": _MAX_ATTEMPTS,
                },
            },
        )
    return end_state(state.step_count)
