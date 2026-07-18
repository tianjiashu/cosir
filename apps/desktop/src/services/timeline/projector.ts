/**
 * 对话 timeline 投影器。
 *
 * 把 turn 记录与 runtime event 投影成 ChatPanel 可直接渲染的显示模型。
 *
 * @module services/timeline/projector
 */

import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";

/** timeline 工具项。 */
export interface TimelineToolItem {
  /** 原始事件 ID。 */
  eventId: string;
  /** 工具名称。 */
  toolName: string;
  /** 工具显示状态。 */
  status: "running" | "completed" | "error";
  /** 可选错误。 */
  error?: string;
}

/** turn 内按事件顺序渲染的 timeline 条目。 */
export type TurnTimelineEntry =
  | { kind: "assistant"; eventId: string; content: string }
  | { kind: "tool"; item: TimelineToolItem }
  | { kind: "status"; eventId: string; eventType: RuntimeEvent["event_type"]; payload: RuntimeEvent["payload"] };

/** 单个 turn 的 timeline 投影。 */
export interface TurnTimelineItem {
  /** 轮次标识符。 */
  turnId: string;
  /** 用户输入。 */
  userText: string;
  /** 按 runtime event 顺序投影后的显示项。 */
  entries: TurnTimelineEntry[];
}

/**
 * 将 turn 与事件投影成稳定 timeline。
 *
 * @param turns - 当前 task 下的轮次列表。
 * @param events - 当前 task 下的 runtime event 列表。
 * @returns 可供组件直接渲染的 turn timeline 列表。
 *
 * @throws 不抛出异常；未知事件类型会被忽略。
 *
 * @sideeffect 无。
 */
export function projectTurnTimeline(turns: TurnRecord[], events: RuntimeEvent[]): TurnTimelineItem[] {
  return turns.map((turn) => {
    const turnEvents = events.filter((event) => event.turn_id === turn.turn_id);
    return {
      turnId: turn.turn_id,
      userText: turn.input_text,
      entries: projectEntries(turnEvents),
    };
  });
}

/**
 * 投影一个 turn 内的运行事件。
 *
 * 将相邻的 `model_output_delta` 事件聚合成一条 assistant 消息，
 * 避免每个 delta 渲染成独立气泡导致界面碎片化。
 * 遇到工具、状态或其他非 delta 事件时，会先 flush 当前 pending 的 assistant 内容，
 * 使后续 delta 从新的 assistant 消息开始聚合。
 *
 * 未识别的事件类型（如 `run_started`、`step_started` 等）不产生渲染条目，
 * 但同样会中断相邻 delta 的聚合。
 * @param events - 单个 turn 下的 runtime event 列表。
 * @returns 可按原始事件顺序渲染的 timeline 条目。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function projectEntries(events: RuntimeEvent[]): TurnTimelineEntry[] {
  const entries: TurnTimelineEntry[] = [];
  let pendingDelta: { eventId: string; content: string } | null = null;

  for (const event of events) {
    if (event.event_type === "model_output_delta") {
      const delta = String(event.payload.delta ?? "");
      if (pendingDelta) {
        pendingDelta.content += delta;
      } else {
        pendingDelta = { eventId: event.event_id, content: delta };
      }
      continue;
    }

    if (pendingDelta) {
      entries.push({ kind: "assistant", eventId: pendingDelta.eventId, content: pendingDelta.content });
      pendingDelta = null;
    }

    const tool = projectTool(event);
    if (tool) {
      entries.push({ kind: "tool", item: tool });
      continue;
    }

    if (
      event.event_type === "run_finished" ||
      event.event_type === "run_failed" ||
      event.event_type === "run_cancelled"
    ) {
      entries.push({ kind: "status", eventId: event.event_id, eventType: event.event_type, payload: event.payload });
    }
  }

  if (pendingDelta) {
    entries.push({ kind: "assistant", eventId: pendingDelta.eventId, content: pendingDelta.content });
  }

  return entries;
}

/**
 * 投影单个工具事件。
 *
 * @param event - runtime event。
 * @returns 工具显示项；非工具事件返回 null。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function projectTool(event: RuntimeEvent): TimelineToolItem | null {
  if (event.event_type === "tool_call_requested" || event.event_type === "tool_call_started") {
    return {
        eventId: event.event_id,
        toolName: String(event.payload.tool_name ?? "未知工具"),
        status: "running" as const,
      };
  }
  if (event.event_type === "tool_call_finished") {
    return {
        eventId: event.event_id,
        toolName: String(event.payload.tool_name ?? "未知工具"),
        status: String(event.payload.status) === "error" ? "error" as const : "completed" as const,
        error: event.payload.error ? String(event.payload.error) : undefined,
      };
  }
  return null;
}
