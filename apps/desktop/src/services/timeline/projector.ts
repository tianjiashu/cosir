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
  /** 后端剥离的索引降级/陈旧提示；有则在结果区顶部展示，避免信息丢失。 */
  notice?: string | null;
  /** diff 布局条目。 */
  diffEntries?: ToolDiffEntry[];
  /** 执行后结构化载荷（治理标记通道，如 output_truncated / artifact_path）。 */
  resultData?: Record<string, unknown>;
  /** 运行期累积的实时输出（仅 execute_terminal 类工具产生；进入终态后清空）。 */
  output?: string;
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
 * timeline 投影累积状态。
 *
 * 设计约束（性能契约）：
 * - `entries` 引用稳定：未受新事件影响的项**保留旧引用**，仅新增/更新的项产生新引用，
 *   使 React 的 memo / 列表 diff 能精确跳过未变化项，避免流式每帧全量重渲染。
 * - `pendingDelta` / `pendingThinking` 为「尚未被后续事件中断」的累积块；流式期持续追加，
 *   被中断（遇到非 delta 事件）时由 {@link flushPending} 定稿进 entries。
 * - `toolByCallId` 为 callId → entries 下标的映射（下标仅追加不删，稳定），
 *   用于把同一工具调用的 started / finished 合并为单条。
 * - `processedEventIds` 为幂等缓存：已投影的 event_id 不再重复处理（回放/重连场景）。
 */
export interface TimelineProjectorState {
  /** 按到达顺序的显示项；引用稳定（未变项沿用旧引用）。 */
  entries: TurnTimelineEntry[];
  /** 仍在累积的 assistant 文本块（被非 delta 事件中断前不落 entries）。 */
  pendingDelta: { eventId: string; content: string } | null;
  /** 仍在累积的 thinking 文本块。 */
  pendingThinking: { eventId: string; content: string } | null;
  /** 本轮是否出现过 model_output_delta（用于判断 final_response 是否冗余）。 */
  hasDeltaStreamed: boolean;
  /** callId → entries 下标，合并同工具调用的 started/finished。 */
  toolByCallId: Map<string, number>;
  /** 已投影 event_id 集合（幂等去重）。 */
  processedEventIds: Set<string>;
}

/** 空投影状态（无事件时复用，避免每次新建）。 */
export const EMPTY_PROJECTION_STATE: TimelineProjectorState = {
  entries: [],
  pendingDelta: null,
  pendingThinking: null,
  hasDeltaStreamed: false,
  toolByCallId: new Map(),
  processedEventIds: new Set(),
};

/**
 * 创建初始投影状态。
 *
 * @returns 空的 {@link TimelineProjectorState}（引用稳定的空数组/空 Map）。
 */
export function createTimelineProjectorState(): TimelineProjectorState {
  return {
    entries: [],
    pendingDelta: null,
    pendingThinking: null,
    hasDeltaStreamed: false,
    toolByCallId: new Map(),
    processedEventIds: new Set(),
  };
}

/**
 * 增量投影：在已有累积状态上应用「新增事件」，返回新状态。
 *
 * 目的（性能核心）:
 *   流式期间每帧只传入「本帧新增的事件」（delta），而非全量 events；
 *   仅新增/更新的项产生新引用，未变项沿用 prev.entries 中的旧引用，
 *   使下游 memo 能跳过未变化项。把「每帧 O(n) 全量重建」降为「每帧 O(delta)」。
 *
 * 幂等性:
 *   - 同一 event_id 重复到达（如回放/重连）时直接跳过，不重复投影。
 *   - 同一 callId 的 tool_call_finished 到达时，更新既有条目引用而非新增。
 *
 * 参数:
 *   prev - 上一帧投影状态（含稳定引用 entries 与幂等缓存）。
 *   events - 本帧新增的事件（delta，非全量）。
 *
 * 返回:
 *   新投影状态；entries 仅含变化项的新引用，未变项沿用 prev 引用。
 *   若 events 为空或全部已处理，直接返回 prev（零分配）。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无（纯函数，不修改 prev）。
 */
export function projectTimelineIncrementally(
  prev: TimelineProjectorState,
  events: RuntimeEvent[],
): TimelineProjectorState {
  if (events.length === 0) {
    return prev;
  }

  // 先判幂等：若本批事件全部已处理过，则零分配返回 prev（避免每帧复制大数组）。
  const hasNew = events.some((e) => !prev.processedEventIds.has(e.event_id));
  if (!hasNew) {
    return prev;
  }

  // 拷贝可变部分；未变项在 push/更新时直接复用 prev.entries[i] 的引用。
  let entries = prev.entries;
  let pendingDelta = prev.pendingDelta;
  let pendingThinking = prev.pendingThinking;
  let hasDeltaStreamed = prev.hasDeltaStreamed;
  const toolByCallId = new Map(prev.toolByCallId);
  const processedEventIds = new Set(prev.processedEventIds);

  const flushPending = () => {
    if (pendingThinking) {
      const thinkingLen = pendingThinking.content.length;
      logInfo("thinking_block_flushed", {
        module: "projector",
        event_id: pendingThinking.eventId,
        content_len: thinkingLen,
        empty: thinkingLen === 0,
      });
      if (pendingThinking.content.trim().length > 0) {
        entries = entries.concat({ kind: "thinking", eventId: pendingThinking.eventId, content: pendingThinking.content });
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
      entries = entries.concat({ kind: "assistant", eventId: pendingDelta.eventId, content: pendingDelta.content });
      pendingDelta = null;
    }
  };

  for (const event of events) {
    if (processedEventIds.has(event.event_id)) {
      continue;
    }
    processedEventIds.add(event.event_id);

    if (event.event_type === "model_thinking_delta") {
      const thinking = String(event.payload.text ?? "");
      if (pendingDelta) {
        entries = entries.concat({ kind: "assistant", eventId: pendingDelta.eventId, content: pendingDelta.content });
        pendingDelta = null;
      }
      // 不可变更新：新建对象而非就地 +=，避免污染调用方仍持有的 prev 状态。
      pendingThinking = pendingThinking
        ? { eventId: pendingThinking.eventId, content: pendingThinking.content + thinking }
        : { eventId: event.event_id, content: thinking };
      continue;
    }

    if (event.event_type === "model_output_delta") {
      const text = String(event.payload.text ?? "");
      if (pendingThinking) {
        entries = entries.concat({ kind: "thinking", eventId: pendingThinking.eventId, content: pendingThinking.content });
        pendingThinking = null;
      }
      // 不可变更新：新建对象而非就地 +=，避免污染调用方仍持有的 prev 状态。
      pendingDelta = pendingDelta
        ? { eventId: pendingDelta.eventId, content: pendingDelta.content + text }
        : { eventId: event.event_id, content: text };
      hasDeltaStreamed = true;
      continue;
    }

    if (event.event_type === "tool_output_delta") {
      // 运行期输出增量：只更新既有工具条目的 output，不打断 pending 文本块，
      // 也不新建孤立条目（started 未到达说明该 delta 无归属，直接丢弃）。
      const payload = event.payload as { tool_call_id?: string; text?: string };
      const callId = payload.tool_call_id ? String(payload.tool_call_id) : "";
      const idx = callId ? toolByCallId.get(callId) : undefined;
      if (idx === undefined) {
        logWarn("tool_output_delta_orphan", {
          module: "projector",
          event_id: event.event_id,
          tool_call_id: callId,
          reason: "未找到对应的 tool_call_started 条目，丢弃该输出增量",
        });
        continue;
      }
      const existing = entries[idx] as Extract<TurnTimelineEntry, { kind: "tool" }>;
      entries = entries.slice();
      entries[idx] = {
        kind: "tool",
        item: { ...existing.item, output: (existing.item.output ?? "") + String(payload.text ?? "") },
      };
      continue;
    }

    flushPending();

    const tool = projectTool(event);
    if (tool) {
      const callId = tool.callId;
      if (callId && toolByCallId.has(callId)) {
        const idx = toolByCallId.get(callId)!;
        const existing = entries[idx] as Extract<TurnTimelineEntry, { kind: "tool" }>;
        // 时序保护：若同 callId 的 finished 已先到达（后端保序前提下理论上不应发生，
        // 但网络重排 / 重连回放可能导致 started 晚到），不得把已完成状态回退为 running。
        // 仅当当前仍为 running（尚未收到终态）时才允许 started 覆写；已终态时保留
        // completed / error，仅补充 arguments / display 等 started 携带的元信息。
        const alreadyTerminal = existing.item.status !== "running";
        const updated: TurnTimelineEntry = {
          kind: "tool",
          item: {
            ...existing.item,
            ...(alreadyTerminal ? {} : { status: tool.status }),
            eventId: tool.eventId,
            error: tool.error,
            resultSummary: tool.resultSummary,
            result: tool.result,
            reason: tool.reason,
            retryable: tool.retryable,
            listEntries: tool.listEntries,
            emptyLabel: tool.emptyLabel,
            diffEntries: tool.diffEntries,
            resultData: tool.resultData,
            // 运行期输出是 xterm 视图的专用通道；进入终态后由静态 <pre> 渲染 result，
            // 此处清空避免两条渲染路径同时持有输出造成重复展示与脏状态。
            output: undefined,
            ...(tool.arguments ? { arguments: tool.arguments } : {}),
            ...(tool.display ? { display: tool.display } : {}),
            // 乱序场景（finished 先到、started 后到）：started 携带的折叠态请求摘要须补全，
            // 否则该工具条目将永久缺失 requestSummary（finished 分支不产出此字段）。
            ...(tool.requestSummary ? { requestSummary: tool.requestSummary } : {}),
          },
        };
        entries = entries.slice();
        entries[idx] = updated;
      } else {
        if (callId) {
          toolByCallId.set(callId, entries.length);
        }
        entries = entries.concat({ kind: "tool", item: tool });
      }
      continue;
    }

    if (
      event.event_type === "run_finished" ||
      event.event_type === "run_failed" ||
      event.event_type === "run_cancelled"
    ) {
      entries = entries.concat({
        kind: "status",
        eventId: event.event_id,
        eventType: event.event_type,
        payload: event.payload,
      });
    }

    if (event.event_type === "final_response") {
      const text = String((event.payload as { text?: unknown }).text ?? "");
      if (text.length > 0 && !hasDeltaStreamed) {
        entries = entries.concat({ kind: "assistant", eventId: event.event_id, content: text });
      }
    }
  }

  return {
    entries,
    pendingDelta,
    pendingThinking,
    hasDeltaStreamed,
    toolByCallId,
    processedEventIds,
  };
}

/**
 * 读取「可见条目」：已定稿 entries + 仍在累积的 pending 块（标记 streaming）。
 *
 * 目的:
 *   TimelineProjectorState.entries 只保存**已定稿**条目，pending 块不写入；
 *   否则每帧把未完成块 concat 进累积状态，会导致同一块被反复追加
 *   （"Hel" / "Hello" / "Hello world" 层层堆叠）。渲染所需的
 *   「定稿 + 进行中」视图在此按需派生，保证累积态干净且幂等。
 *
 * 参数:
 *   state - 当前投影状态。
 *
 * 返回:
 *   渲染用条目数组；无 pending 时直接返回 state.entries 原引用（零分配，引用稳定）。
 *
 * 异常:
 *   不抛出。
 *
 * @sideeffect 无（纯函数）。
 */
export function selectVisibleEntries(state: TimelineProjectorState): TurnTimelineEntry[] {
  const { entries, pendingThinking, pendingDelta } = state;
  const hasThinking = Boolean(pendingThinking && pendingThinking.content.trim().length > 0);
  if (!hasThinking && !pendingDelta) {
    return entries;
  }
  const visible = entries.slice();
  if (pendingThinking && hasThinking) {
    visible.push({
      kind: "thinking",
      eventId: pendingThinking.eventId,
      content: pendingThinking.content,
      streaming: true,
    });
  }
  if (pendingDelta) {
    visible.push({
      kind: "assistant",
      eventId: pendingDelta.eventId,
      content: pendingDelta.content,
      streaming: true,
    });
  }
  return visible;
}

/**
 * 将 turn 与事件投影成稳定 timeline。
 *
 * 实现为「从零增量投影」的便捷封装：先建空状态，再把全部 events 应用一遍增量逻辑，
 * 与流式增量路径共用同一投影语义，保证首屏/回放与流式产出完全一致的项结构（幂等）。
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
    const state = projectTimelineIncrementally(EMPTY_PROJECTION_STATE, turnEvents);
    const entries = selectVisibleEntries(state).slice();
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
      notice: projection.notice,
      diffEntries: projection.diffEntries,
      resultData: payload.data && typeof payload.data === "object" ? payload.data : undefined,
    };
  }
  return null;
}

export type { ToolDiffEntry, ToolDisplayHints, ToolListEntry };
