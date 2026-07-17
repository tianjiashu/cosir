"""解析 OpenAI 兼容的聊天补全流式分块。"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List

from app.models.base import ModelDelta
from app.tools.schemas import ToolCall


@dataclass
class ToolCallAccumulator:
    """为一个工具调用累积流式传输的工具调用分片。

    参数:
        call_id: 服务商工具调用标识符。
        name: 从流中收集到的工具名分片。
        arguments: 从流中收集到的 JSON 参数分片。

    返回:
        用于单个流式工具调用的可变累加器。

    异常:
        无。

    副作用:
        无。
    """

    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class OpenAIStreamState:
    """跨流式分块追踪解析器状态。

    参数:
        tool_calls: 以服务商索引为键的部分工具调用。

    返回:
        可变的解析器状态。

    异常:
        无。

    副作用:
        无。
    """

    tool_calls: Dict[int, ToolCallAccumulator] = field(default_factory=dict)


def parse_openai_sse_lines(lines: Iterable[str]) -> Iterator[ModelDelta]:
    """将 OpenAI 兼容的 SSE 行解析为模型增量。

    参数:
        lines: 来自 SSE 响应体的原始文本行可迭代对象。

    生成:
        表示文本增量、工具调用和结束标记的 ModelDelta 值。

    异常:
        ValueError: 如果已完成的工具调用包含非法或非对象的 JSON 参数。

    副作用:
        无。
    """

    state = OpenAIStreamState()
    for line in lines:
        yield from parse_openai_sse_line(line, state)


def parse_openai_sse_line(line: str, state: OpenAIStreamState) -> Iterator[ModelDelta]:
    """解析一行 OpenAI 兼容的 SSE 行。

    参数:
        line: 原始 SSE 行，通常以 ``data:`` 开头。
        state: 用于累积流式工具调用分片的解析器状态。

    生成:
        该行解析出的模型增量。

    异常:
        ValueError: 如果已完成的工具调用包含非法或非对象的 JSON 参数。

    副作用:
        当存在工具调用分片时修改 ``state``。
    """

    stripped = line.strip()
    if not stripped or not stripped.startswith("data:"):
        return

    data = stripped.removeprefix("data:").strip()
    if data == "[DONE]":
        yield from _flush_tool_calls(state)
        yield ModelDelta(text="", is_final=True)
        return

    payload = json.loads(data)
    for choice in payload.get("choices", []):
        delta = choice.get("delta", {})
        content = delta.get("content")
        if content:
            yield ModelDelta(text=content)

        for tool_call_delta in delta.get("tool_calls", []) or []:
            _accumulate_tool_call(state, tool_call_delta)

        if choice.get("finish_reason") == "tool_calls":
            yield from _flush_tool_calls(state)
        elif choice.get("finish_reason") == "stop":
            yield ModelDelta(text="", is_final=True)


def _accumulate_tool_call(
    state: OpenAIStreamState,
    tool_call_delta: Dict[str, Any],
) -> None:
    """将一个流式工具调用增量累积进解析器状态。

    参数:
        state: 需要修改的解析器状态。
        tool_call_delta: 服务商工具调用增量对象。

    返回:
        无。

    异常:
        无。

    副作用:
        修改工具调用累加器状态。
    """

    index = int(tool_call_delta.get("index", 0))
    accumulator = state.tool_calls.setdefault(index, ToolCallAccumulator())
    accumulator.call_id = accumulator.call_id or tool_call_delta.get("id") or ""
    function_delta = tool_call_delta.get("function", {})
    accumulator.name += function_delta.get("name") or ""
    accumulator.arguments += function_delta.get("arguments") or ""


def _flush_tool_calls(state: OpenAIStreamState) -> Iterator[ModelDelta]:
    """从解析器状态中冲刷已完成的工具调用。

    参数:
        state: 包含已累积工具调用的解析器状态。

    生成:
        包含 ToolCall 对象的 ModelDelta 值。

    异常:
        ValueError: 如果累积的 JSON 参数无法被解析为对象。

    副作用:
        清空已累积的工具调用状态。
    """

    completed: List[ToolCallAccumulator] = [
        state.tool_calls[index] for index in sorted(state.tool_calls)
    ]
    state.tool_calls.clear()
    for accumulator in completed:
        arguments = json.loads(accumulator.arguments or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
        yield ModelDelta(
            text="",
            tool_call=ToolCall(
                tool_name=accumulator.name,
                arguments=arguments,
                call_id=accumulator.call_id,
            ),
        )
