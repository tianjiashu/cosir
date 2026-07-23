"""Generate TypeScript runtime event types from backend payload models."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from types import UnionType
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "apps" / "backend"
OUTPUT_PATH = REPO_ROOT / "apps" / "shared" / "ts" / "events.ts"

sys.path.insert(0, str(BACKEND_ROOT))

from app.models.enums.event_type import EventType  # noqa: E402
from app.models.payload import EVENT_PAYLOAD_MODELS, ModelToolCallPayload  # noqa: E402


def main() -> None:
    """生成前端 runtime event TypeScript 类型文件。

    参数:
        无。

    返回:
        无。

    异常:
        RuntimeError: 当后端事件枚举没有对应 payload 模型时抛出。

    副作用:
        覆盖写入 ``apps/shared/ts/events.ts``。
    """

    event_types = list(EventType)
    missing = [
        event_type.value
        for event_type in event_types
        if event_type not in EVENT_PAYLOAD_MODELS
    ]
    if missing:
        raise RuntimeError(
            f"missing payload models for event types: {', '.join(missing)}"
        )

    OUTPUT_PATH.write_text(render_typescript(event_types), encoding="utf-8")


def render_typescript(event_types: list[EventType]) -> str:
    """渲染完整 TypeScript 文件内容。

    参数:
        event_types: 后端事件枚举成员，顺序决定生成文件中的类型顺序。

    返回:
        可直接写入 ``events.ts`` 的文本。

    异常:
        无。

    副作用:
        无。
    """

    payload_models = [EVENT_PAYLOAD_MODELS[event_type] for event_type in event_types]
    interfaces = "\n\n".join(
        render_interface(model) for model in unique_models(payload_models)
    )
    event_union = "\n".join(f'  | "{event_type.value}"' for event_type in event_types)
    payload_map = "\n".join(
        f"  {event_type.value}: {EVENT_PAYLOAD_MODELS[event_type].__name__};"
        for event_type in event_types
    )
    return f"""/**
 * 后端运行时事件类型定义。
 *
 * 本文件由 `scripts/generate_runtime_event_ts.py` 从后端 Pydantic payload 模型生成。
 * 不要手动修改；请先更新 `apps/backend/app/models/payload/` 后重新生成。
 *
 * @module shared/events
 */

/** 后端发出的运行时事件类型。 */
export type RuntimeEventType =
{event_union};

/** 所有运行时事件 payload 都是 JSON object。 */
export type RuntimeEventPayloadObject = Record<string, unknown>;

{interfaces}

/** event_type 到 payload 类型的映射。 */
export interface RuntimeEventPayloadMap {{
{payload_map}
}}

/** SSE 传输的运行时事件信封，对应后端 `RuntimeEvent.to_dict()`。 */
export interface RuntimeEventEnvelope<T extends RuntimeEventType = RuntimeEventType> {{
  /** 唯一的事件标识符（UUID）。 */
  event_id: string;
  /** 稳定的、机器可读的事件类型。 */
  event_type: T;
  /** 关联的任务标识符。 */
  task_id: string;
  /** 关联的轮次标识符。 */
  turn_id?: string | null;
  /** 当前单次运行流内的排序号；不是 task 级持久序号。 */
  sequence?: number;
  /** 可选的用户可读消息标识符。 */
  message_id?: string | null;
  /** 可选的工具调用标识符。 */
  tool_call_id?: string | null;
  /** 事件创建时的 UTC 时间戳（ISO-8601）。 */
  created_at: string;
  /** 因 event_type 而异的载荷字典。 */
  payload: RuntimeEventPayloadMap[T];
}}

/** 后端 SSE 运行时事件联合类型。 */
export type RuntimeEvent = {{
  [T in RuntimeEventType]: RuntimeEventEnvelope<T>;
}}[RuntimeEventType];
"""


def unique_models(payload_models: Sequence[type[BaseModel]]) -> list[type[BaseModel]]:
    """返回生成 TS interface 所需的去重模型列表。

    参数:
        payload_models: event_type 映射中的 payload 模型列表。

    返回:
        去重后的模型列表，嵌套模型排在事件 payload 模型之前。

    异常:
        无。

    副作用:
        无。
    """

    models: list[type[BaseModel]] = [ModelToolCallPayload]
    for model in payload_models:
        if model not in models:
            models.append(model)
    return models


def render_interface(model: type[BaseModel]) -> str:
    """把一个 Pydantic 模型渲染成 TypeScript interface。

    参数:
        model: 待渲染的 Pydantic 模型类。

    返回:
        TypeScript interface 文本。

    异常:
        无。

    副作用:
        无。
    """

    lines = [f"export interface {model.__name__} extends RuntimeEventPayloadObject {{"]
    for field_name, field_info in model.model_fields.items():
        optional = "?" if not field_info.is_required() else ""
        lines.append(f"  {field_name}{optional}: {ts_type(field_info.annotation)};")
    lines.append("}")
    return "\n".join(lines)


def ts_type(annotation: Any) -> str:
    """把 Python 类型注解转换成 TypeScript 类型表达式。

    参数:
        annotation: Pydantic 字段上的 Python 类型注解。

    返回:
        对应的 TypeScript 类型文本。

    异常:
        无。

    副作用:
        无。
    """

    origin = get_origin(annotation)
    args = get_args(annotation)
    if annotation is Any:
        return "unknown"
    if annotation is str:
        return "string"
    if annotation is int or annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"
    if origin is Literal:
        return " | ".join(render_literal(value) for value in args)
    if origin is list:
        return f"{ts_type(args[0])}[]"
    if origin is dict:
        return "Record<string, unknown>"
    if origin in (UnionType, getattr(sys.modules["typing"], "Union", object())):
        return " | ".join(ts_type(arg) for arg in args)
    if annotation is type(None):
        return "null"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__
    return "unknown"


def render_literal(value: object) -> str:
    """渲染 TypeScript 字面量类型。

    参数:
        value: Python 字面量值。

    返回:
        TypeScript 字面量类型文本。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(value, str):
        return f'"{value}"'
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


if __name__ == "__main__":
    main()
