/**
 * 对话 timeline 投影器。
 *
 * 把 turn 记录与 runtime event 投影成 ChatPanel 可直接渲染的显示模型。
 * 工具相关的摘要与条目由共享渲染层（`@shared/toolDisplayRules`）按工具名规则生成，
 * 本模块只负责把事件数据喂给渲染层并装配成稳定结构，不承载任何渲染逻辑。
 *
 * @module services/timeline/projector
 */

import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import {
  type ToolDiffEntry,
  type ToolListEntry,
  projectToolRequestSummary,
  projectToolResult,
} from "@shared/toolDisplayRules";
import { type ToolDisplayHints, toToolDisplayHints } from "@shared/toolDisplay";
import { logInfo, logWarn } from "../../lib/logger";

/** 工具展示提示（后端静态声明，已投影为 camelCase；不含任何摘要文本）。 */
export interface ToolDisplayInfo {
  /** 动作名，如 “读取”。 */
  verb: string;
  /** lucide 图标名，如 “eye”。 */
  icon: string;
  /** 是否可展开。 */
  expandable: boolean;
  /** 展开态布局：none/details/list/diff/write/terminal。 */
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
  /** 工具调用参数（来自 `tool_call_started`，用于渲染折叠态摘要与展开态参数）。 */
  arguments?: Record<string, unknown>;
  /** 工具调用唯一 ID，用于把 started / finished 事件合并为同一条目。 */
  callId?: string;
  /** 工具展示静态提示；来自后端，未声明时缺省，前端降级为通用展示。 */
  display?: ToolDisplayInfo;
  /** 折叠态请求摘要（前端按参数渲染）。 */
  requestSummary?: string;
  /** 执行后结果摘要（成功时，来自共享渲染层）。 */
  resultSummary?: string;
  /** 执行后完整结果正文（模型所见，展开态渲染）。 */
  result?: string;
  /** 失败辅因：为什么失败 + 如何修正（error 为主因）。 */
  reason?: string;
  /** 失败是否可重试（瞬态错误 true / 需先修正参数 false）。 */
  retryable?: boolean;
  /** list 布局条目（前端按字段形状渲染）。 */
  listEntries?: ToolListEntry[];
  /** list 布局空态文案。 */
  emptyLabel?: string | null;
  /** diff 布局条目。 */
  diffEntries?: ToolDiffEntry[];
  /** 执行后结构化载荷（治理标记通道，如 output_truncated / artifact_path）。 */
  resultData?: Record<string, unknown>;
}

/** turn 内按事件顺序渲染的 timeline 条目。 */
export type TurnTimelineEntry =
  | { kind: "assistant"; eventId: string; content: string; streaming?: boolean }
  | { kind: "thinking"; eventId: string; content: string; streaming?: boolean }
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
 *
 * 块级 streaming 语义：被后续事件中断而定稿的块不带 `streaming`；
 * 事件流耗尽时仍在累积的块标记 `streaming: true`。由于 `run_finished` /
 * `run_failed` / `run_cancelled` 属于非 delta 事件，会先触发 flush，
 * 因此运行结束后不会残留 `streaming: true` 的悬空块。
 * @param events - 单个 turn 下的 runtime event 列表。
 * @returns 可按原始事件顺序渲染的 timeline 条目；进行中的块带 `streaming: true`。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function projectEntries(events: RuntimeEvent[]): TurnTimelineEntry[] {
  const entries: TurnTimelineEntry[] = [];
  let pendingDelta: { eventId: string; content: string } | null = null;
  let pendingThinking: { eventId: string; content: string } | null = null;
  // 本 turn 是否出现过 model_output_delta。它不随 flushPending 重置，
  // 用于判断 final_response 是否为冗余（delta 已聚合过同文本）从而跳过，避免重复渲染。
  let hasDeltaStreamed = false;
  // 工具条目按 callId 合并：started 携参数创建条目，finished 更新其状态，
  // 避免同一工具调用产生「运行中 + 完成」两条碎片条目。
  const toolByCallId = new Map<string, number>();

  const flushPending = () => {
    if (pendingThinking) {
      // 调试：思考块产出时记录内容长度，空白块（len=0）是"深度思考空白"的直接嫌疑点。
      const thinkingLen = pendingThinking.content.length;
      logInfo("thinking_block_flushed", {
        module: "projector",
        event_id: pendingThinking.eventId,
        content_len: thinkingLen,
        empty: thinkingLen === 0,
      });
      // 防御：跳过空白或纯空白字符的思考块，避免渲染空的"深度思考"壳。
      // 上游可能发送仅含换行/空格的 thinking delta（如 DeepSeek reasoning 的分隔符），
      // 累积后经 flushPending 产出无意义的空白块。
      if (pendingThinking.content.trim().length > 0) {
        entries.push({ kind: "thinking", eventId: pendingThinking.eventId, content: pendingThinking.content });
      } else {
        logWarn("thinking_block_skipped", {
          module: "projector",
          event_id: pendingThinking.eventId,
          content_len: thinkingLen,
          reason: "思考块内容为纯空白，跳过以避免渲染空壳",
        });
      }
      pendingThinking = null;
    }
    if (pendingDelta) {
      entries.push({ kind: "assistant", eventId: pendingDelta.eventId, content: pendingDelta.content });
      pendingDelta = null;
    }
  };

  /**
   * 在事件流末尾把仍未被中断的 pending 块投影为「进行中」条目。
   *
   * 与 `flushPending` 的区别：`flushPending` 处理的是被后续事件中断、
   * 已经定稿的块（不带 streaming）；本函数处理的是流尚未结束、
   * 后续 delta 仍会继续追加的块，因此标记 `streaming: true`，
   * 供渲染层做「正在输出」的排版处理（如光标、去抖动）。
   *
   * 参数:
   *   无。
   *
   * 返回:
   *   无返回值。
   *
   * @throws 不抛出异常。
   *
   * @sideeffect 向闭包内的 `entries` 追加条目；**不清空** `pendingDelta` /
   *   `pendingThinking`，以便下一次重新投影时能从已累积状态继续。
   */
  const flushPendingFinal = () => {
    if (pendingThinking && pendingThinking.content.trim().length > 0) {
      entries.push({
        kind: "thinking",
        eventId: pendingThinking.eventId,
        content: pendingThinking.content,
        streaming: true,
      });
    }
    if (pendingDelta) {
      entries.push({
        kind: "assistant",
        eventId: pendingDelta.eventId,
        content: pendingDelta.content,
        streaming: true,
      });
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
      hasDeltaStreamed = true;
      continue;
    }

    flushPending();

    const tool = projectTool(event);
    if (tool) {
      const callId = tool.callId;
      if (callId && toolByCallId.has(callId)) {
        // 同一工具调用已有 started 条目，仅更新其状态/错误/结果字段/id，保留参数
        const idx = toolByCallId.get(callId)!;
        const existing = entries[idx] as Extract<TurnTimelineEntry, { kind: "tool" }>;
        existing.item.status = tool.status;
        existing.item.eventId = tool.eventId;
        existing.item.error = tool.error;
        existing.item.resultSummary = tool.resultSummary;
        existing.item.result = tool.result;
        existing.item.reason = tool.reason;
        existing.item.retryable = tool.retryable;
        existing.item.listEntries = tool.listEntries;
        existing.item.emptyLabel = tool.emptyLabel;
        existing.item.diffEntries = tool.diffEntries;
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

    // final_response 携带 Agent 最终完整文本回复（后端 model 节点在流结束时发出）。
    // 若本轮已有 delta 流式累积（hasDeltaStreamed），该文本与 delta 聚合内容一致，
    // 为避免重复渲染，此处跳过；只有「无 delta 流、仅靠 final_response 携带文本」时才投影为 assistant 条目。
    // 注意：判断依据是 hasDeltaStreamed（不随 flushPending 重置），而非 pendingDelta（flush 后恒为 null）。
    if (event.event_type === "final_response") {
      const text = String((event.payload as { text?: unknown }).text ?? "");
      if (text.length > 0 && !hasDeltaStreamed) {
        entries.push({ kind: "assistant", eventId: event.event_id, content: text });
      }
    }
  }

  flushPendingFinal();

  return entries;
}

/**
 * 投影单个工具事件。
 *
 * 工具相关的摘要与条目委托给共享渲染层（`projectToolRequestSummary` /
 * `projectToolResult`），本函数只装配事件数据并回填渲染结果，不含渲染分支。
 *
 * @param event - runtime event。
 * @returns 工具显示项；非工具事件返回 null。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function projectTool(event: RuntimeEvent): TimelineToolItem | null {
  if (event.event_type === "tool_call_started") {
    const payload = event.payload as {
      tool_name?: string;
      arguments?: Record<string, unknown>;
      tool_call_id?: string | null;
      display?: Record<string, unknown> | null;
    };
    const toolName = String(payload.tool_name ?? "未知工具");
    const arguments_ = payload.arguments ? (payload.arguments as Record<string, unknown>) : undefined;
    return {
      eventId: event.event_id,
      toolName,
      status: "running" as const,
      arguments: arguments_,
      callId: payload.tool_call_id ? String(payload.tool_call_id) : undefined,
      display: toToolDisplayHints(payload.display),
      requestSummary: projectToolRequestSummary(toolName, arguments_),
    };
  }
  if (event.event_type === "tool_call_finished") {
    const payload = event.payload as {
      tool_name?: string;
      status?: string;
      error?: string;
      tool_call_id?: string | null;
      content?: string | null;
      reason?: string;
      retryable?: boolean;
      data?: Record<string, unknown>;
    };
    const toolName = String(payload.tool_name ?? "未知工具");
    const projection = projectToolResult(toolName, payload.data);
    return {
      eventId: event.event_id,
      toolName,
      status: String(payload.status) === "error" ? "error" as const : "completed" as const,
      error: payload.error ? String(payload.error) : undefined,
      callId: payload.tool_call_id ? String(payload.tool_call_id) : undefined,
      resultSummary: projection.summary ?? undefined,
      result: payload.content ? String(payload.content) : undefined,
      reason: payload.reason ? String(payload.reason) : undefined,
      retryable: typeof payload.retryable === "boolean" ? payload.retryable : undefined,
      listEntries: projection.listEntries,
      emptyLabel: projection.emptyLabel,
      diffEntries: projection.diffEntries,
      resultData: payload.data && typeof payload.data === "object" ? payload.data : undefined,
    };
  }
  return null;
}

export type { ToolDiffEntry, ToolDisplayHints, ToolListEntry };
