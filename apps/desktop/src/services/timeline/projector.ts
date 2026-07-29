/**
 * 对话 timeline 投影器。
 *
 * 把 turn 记录与 runtime event 投影成 ChatPanel 可直接渲染的显示模型。
 *
 * @module services/timeline/projector
 */

import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";

/** 工具点击动作（来自后端 `ToolDefinition.display`，已投影）。 */
export interface ToolDisplayClickAction {
  /** 动作类型，如 `open_file`，前端据此分发行为。 */
  action: string;
  /** 动作目标，已结合本次调用参数渲染，如文件路径。 */
  target: string;
}

/** 工具展示提示（来自后端 `ToolDefinition.display.render`，已投影为 camelCase）。 */
export interface ToolDisplayInfo {
  /** 动作名，如 “读取”。 */
  verb: string;
  /** lucide 图标名，如 “eye”。 */
  icon: string;
  /** 折叠态摘要文本（已含路径/行范围等）。 */
  summary: string;
  /** 展开态优先展示的参数 key 顺序。 */
  detailKeys: string[];
  /** 可选点击动作；为空表示不可点击。 */
  clickAction: ToolDisplayClickAction | null;
  /** 是否可展开（默认 true）；read_file 等显式 false。 */
  expandable: boolean;
  /** 展开态布局：none/details/list/diff/write/terminal（默认 details）。 */
  expandLayout: string;
}

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
  /** 工具调用参数（来自 `tool_call_requested`，用于前端展示文件路径/行范围等）。 */
  arguments?: Record<string, unknown>;
  /** 工具调用唯一 ID，用于把 requested / finished 事件合并为同一条目。 */
  callId?: string;
  /** 工具展示提示；来自后端，未声明时缺省，前端降级为通用展示。 */
  display?: ToolDisplayInfo;
  /** 执行后结果摘要（成功时，来自后端 result_summary_template 渲染）。 */
  resultSummary?: string;
  /** 执行后完整结果正文（模型所见，展开态渲染）。 */
  result?: string;
  /** 失败辅因：为什么失败 + 如何修正（error 为主因）。 */
  reason?: string;
  /** 失败是否可重试（瞬态错误 true / 需先修正参数 false）。 */
  retryable?: boolean;
  /** 执行后结构化载荷（通用透传）。 */
  resultData?: Record<string, unknown>;
}

/** turn 内按事件顺序渲染的 timeline 条目。 */
export type TurnTimelineEntry =
  | { kind: "assistant"; eventId: string; content: string }
  | { kind: "thinking"; eventId: string; content: string }
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
    const entries = projectEntries(turnEvents);
    if (entries.length === 0 && turn.response_text) {
      entries.push({
        kind: "assistant",
        eventId: `turn-response-${turn.turn_id}`,
        content: turn.response_text,
      });
    }
    return {
      turnId: turn.turn_id,
      userText: turn.input_text,
      entries,
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
  let pendingThinking: { eventId: string; content: string } | null = null;
  // 工具条目按 callId 合并：requested 携参数创建条目，finished 更新其状态，
  // 避免同一工具调用产生「运行中 + 完成」两条碎片条目。
  const toolByCallId = new Map<string, number>();

  const flushPending = () => {
    if (pendingThinking) {
      entries.push({ kind: "thinking", eventId: pendingThinking.eventId, content: pendingThinking.content });
      pendingThinking = null;
    }
    if (pendingDelta) {
      entries.push({ kind: "assistant", eventId: pendingDelta.eventId, content: pendingDelta.content });
      pendingDelta = null;
    }
  };

  for (const event of events) {
    if (event.event_type === "model_thinking_delta") {
      const thinking = String(event.payload.text ?? "");
      // 思考与回答交错时，先把已累积的回答 flush，再开始新的思考块
      if (pendingDelta) {
        entries.push({ kind: "assistant", eventId: pendingDelta.eventId, content: pendingDelta.content });
        pendingDelta = null;
      }
      if (pendingThinking) {
        pendingThinking.content += thinking;
      } else {
        pendingThinking = { eventId: event.event_id, content: thinking };
      }
      continue;
    }

    if (event.event_type === "model_output_delta") {
      const text = String(event.payload.text ?? "");
      // 回答开始前先把思考块 flush，保证思考显示在前
      if (pendingThinking) {
        entries.push({ kind: "thinking", eventId: pendingThinking.eventId, content: pendingThinking.content });
        pendingThinking = null;
      }
      if (pendingDelta) {
        pendingDelta.content += text;
      } else {
        pendingDelta = { eventId: event.event_id, content: text };
      }
      continue;
    }

    flushPending();

    const tool = projectTool(event);
    if (tool) {
      const callId = tool.callId;
      if (callId && toolByCallId.has(callId)) {
        // 同一工具调用已有 requested 条目，仅更新其状态/错误/结果字段/id，保留参数
        const idx = toolByCallId.get(callId)!;
        const existing = entries[idx] as Extract<TurnTimelineEntry, { kind: "tool" }>;
        existing.item.status = tool.status;
        existing.item.error = tool.error;
        existing.item.eventId = tool.eventId;
        existing.item.resultSummary = tool.resultSummary;
        existing.item.result = tool.result;
        existing.item.reason = tool.reason;
        existing.item.retryable = tool.retryable;
        existing.item.resultData = tool.resultData;
        if (tool.arguments) {
          existing.item.arguments = tool.arguments;
        }
        if (tool.display) {
          existing.item.display = tool.display;
        }
      } else {
        if (callId) {
          toolByCallId.set(callId, entries.length);
        }
        entries.push({ kind: "tool", item: tool });
      }
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

  flushPending();

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
    const payload = event.payload as {
      tool_name?: string;
      arguments?: Record<string, unknown>;
      tool_call_id?: string | null;
      display?: Record<string, unknown> | null;
    };
    return {
        eventId: event.event_id,
        toolName: String(payload.tool_name ?? "未知工具"),
        status: "running" as const,
        arguments: payload.arguments ? (payload.arguments as Record<string, unknown>) : undefined,
        callId: payload.tool_call_id ? String(payload.tool_call_id) : undefined,
        display: toolDisplayFromPayload(payload.display),
      };
  }
  if (event.event_type === "tool_call_finished") {
    const payload = event.payload as {
      tool_name?: string;
      status?: string;
      error?: string;
      tool_call_id?: string | null;
      summary?: string | null;
      content?: string | null;
      reason?: string;
      retryable?: boolean;
      data?: Record<string, unknown>;
    };
    return {
        eventId: event.event_id,
        toolName: String(payload.tool_name ?? "未知工具"),
        status: String(payload.status) === "error" ? "error" as const : "completed" as const,
        error: payload.error ? String(payload.error) : undefined,
        callId: payload.tool_call_id ? String(payload.tool_call_id) : undefined,
        resultSummary: payload.summary ? String(payload.summary) : undefined,
        result: payload.content ? String(payload.content) : undefined,
        reason: payload.reason ? String(payload.reason) : undefined,
        retryable: typeof payload.retryable === "boolean" ? payload.retryable : undefined,
        resultData: payload.data && typeof payload.data === "object" ? payload.data : undefined,
      };
  }
  return null;
}

/**
 * 把后端透传的展示提示投影成前端使用的结构。
 *
 * 后端 `display` 为 snake_case 字典且字段可选；本函数做类型收窄与 camelCase
 * 转换，缺字段时给出安全默认值，保证 `ToolCallCard` 可无分支消费。
 *
 * 参数:
 *   raw - 事件 payload 中的 `display` 字段（可能为 undefined / null / 非对象）。
 *
 * 返回:
 *   转换后的 `ToolDisplayInfo`；输入非法时返回 undefined。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function toolDisplayFromPayload(raw: unknown): ToolDisplayInfo | undefined {
  if (!raw || typeof raw !== "object") {
    return undefined;
  }
  const display = raw as Record<string, unknown>;
  const clickActionRaw = display.click_action;
  let clickAction: ToolDisplayClickAction | null = null;
  if (clickActionRaw && typeof clickActionRaw === "object") {
    const ca = clickActionRaw as Record<string, unknown>;
    if (typeof ca.action === "string" && typeof ca.target === "string") {
      clickAction = { action: ca.action, target: ca.target };
    }
  }
  const detailKeys = Array.isArray(display.detail_keys)
    ? (display.detail_keys as unknown[]).filter((key) => typeof key === "string") as string[]
    : [];
  return {
    verb: typeof display.verb === "string" ? display.verb : "",
    icon: typeof display.icon === "string" ? display.icon : "wrench",
    summary: typeof display.summary === "string" ? display.summary : "",
    detailKeys,
    clickAction,
    expandable: typeof display.expandable === "boolean" ? display.expandable : true,
    expandLayout: typeof display.expand_layout === "string" ? display.expand_layout : "details",
  };
}
