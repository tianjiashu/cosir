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

/** Delegation lifecycle status projected for the turn timeline. */
export type TimelineDelegationStatus =
  | "pending"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

/** Delegation lifecycle entry shown in the parent turn timeline. */
export interface TimelineDelegationItem {
  /** Source event id for the latest projected lifecycle event. */
  eventId: string;
  /** Stable delegation id used to merge lifecycle events. */
  delegationId: string;
  /** Parent turn that requested the delegation. */
  parentTurnId: string;
  /** Child turn id once the backend creates the child run. */
  childTurnId?: string;
  /** Child AgentProfile id. */
  childAgentId: string;
  /** Display-only delegation category. */
  delegationType: string;
  /** Current lifecycle status. */
  status: TimelineDelegationStatus;
  /** Successful terminal summary. */
  summary?: string;
  /** Failed or cancelled terminal reason. */
  error?: string;
  /**
   * 并发组规模：同 parent turn 下同时处于 running 的 delegation 数量。
   * 仅当数量 >= 2（构成并发组）时附加；非并发（或组仅 1）时为 undefined。
   */
  concurrencyGroupSize?: number;
  /**
   * 并发组内序号：该 delegation 在并发组中的稳定位置（从 0 起，按 running 到达顺序）。
   * 仅当 `concurrencyGroupSize >= 2` 时附加；非并发时为 undefined。
   */
  concurrencyIndex?: number;
}

/** turn 内按事件顺序渲染的 timeline 条目。 */
export type TurnTimelineEntry =
  | { kind: "assistant"; eventId: string; content: string; streaming?: boolean }
  | { kind: "thinking"; eventId: string; content: string; streaming?: boolean }
  | { kind: "tool"; item: TimelineToolItem }
  | { kind: "delegation"; item: TimelineDelegationItem }
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
  /** 终态回复（final_response）是否已到达并被权威定稿。 */
  finalResponseReceived: boolean;
  /** callId → entries 下标，合并同工具调用的 started/finished。 */
  toolByCallId: Map<string, number>;
  /** delegationId → entries index, used to merge lifecycle events. */
  delegationById: Map<string, number>;
  /**
   * parentTurnId → 当前处于 running 的 delegationId 集合。
   * 用于派生并发组信息：同 parent 下同时 running 的 delegation 数量即集合规模。
   * 幂等保证：本集合随 delegation 生命周期事件维护（到达即加入、进入终态即移除），
   * 配合 `processedEventIds` 的 event_id 去重，乱序/回放到达不漂移计数。
   */
  concurrencyByParentTurn: Map<string, Set<string>>;
  /** 已投影 event_id 集合（幂等去重）。 */
  processedEventIds: Set<string>;
}

/** 空投影状态（无事件时复用，避免每次新建）。 */
export const EMPTY_PROJECTION_STATE: TimelineProjectorState = {
  entries: [],
  pendingDelta: null,
  pendingThinking: null,
  hasDeltaStreamed: false,
  finalResponseReceived: false,
  toolByCallId: new Map(),
  delegationById: new Map(),
  concurrencyByParentTurn: new Map(),
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
    finalResponseReceived: false,
    toolByCallId: new Map(),
    delegationById: new Map(),
    concurrencyByParentTurn: new Map(),
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
 *   对 `prev` 中所有可变结构（entries 数组、pendingDelta/pendingThinking 对象、
 *   toolByCallId/delegationById/concurrencyByParentTurn 映射、processedEventIds 集合）
 *   均做拷贝后再改；其中 `concurrencyByParentTurn` 为「外层 Map + 内层 Set」两层结构，
 *   函数开头仅浅拷贝外层 Map，内层 Set 仍与 prev 共享引用，因此任何对并发集合的修改
 *   都需先拷贝内层 Set（`new Set(prevSet)`）再 set 回 Map，绝不就地 mutate 内层 Set，
 *   以保证同一 prev 可被多次安全分叉投影而不互相污染（详见 delegation 分支实现）。
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
  let finalResponseReceived = prev.finalResponseReceived;
  const toolByCallId = new Map(prev.toolByCallId);
  const delegationById = new Map(prev.delegationById);
  const concurrencyByParentTurn = new Map(prev.concurrencyByParentTurn);
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
      // 终态回复已被 final_response 权威定稿（典型：网络重排下 final_response 先到、
      // 段末 delta 迟到）。此时 final_response 文本已是完整回复，迟到 delta 为冗余，
      // 直接丢弃，避免「final_response 完整文本 + 迟到 delta 累积文本」两段重复展示。
      if (finalResponseReceived) {
        continue;
      }
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

    const delegation = projectDelegation(event);
    if (delegation) {
      const parentTurnId = delegation.parentTurnId;
      const did = delegation.delegationId;

      // 维护并发集合：到达即加入；进入终态则从 running 集合移除（不再并发）。
      // 不可变更新：从 Map 取到的内层 Set 仍与 prev 共享引用，必须拷贝后再改，
      // 严禁对原 Set 就地 add/delete，否则会污染调用方仍持有的 prev 状态。
      const prevRunningSet = concurrencyByParentTurn.get(parentTurnId);
      const nextRunningSet = new Set(prevRunningSet);
      if (isDelegationTerminal(delegation.status)) {
        nextRunningSet.delete(did);
      } else {
        nextRunningSet.add(did);
      }
      concurrencyByParentTurn.set(parentTurnId, nextRunningSet);

      const idx = delegationById.get(did);
      if (idx !== undefined) {
        const existing = entries[idx] as Extract<TurnTimelineEntry, { kind: "delegation" }>;
        entries = entries.slice();
        entries[idx] = {
          kind: "delegation",
          item: attachConcurrency(mergeDelegation(existing.item, delegation), concurrencyByParentTurn),
        };
      } else {
        delegationById.set(did, entries.length);
        entries = entries.concat({
          kind: "delegation",
          item: attachConcurrency(delegation, concurrencyByParentTurn),
        });
      }

      // 当前 delegation 的到达/终态改变了集合规模，需同步重算同 parent 下
      // 其它已投影 delegation 条目的并发字段（它们可能因此进入或退出并发组）。
      // 仅当并发字段实际变化时才产生新引用，避免无谓击穿 memo。
      entries = reattachConcurrencyForParent(entries, parentTurnId, concurrencyByParentTurn);
      continue;
    }

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
            // 索引降级/陈旧提示（notice）仅由 finished 投影产出，合并时必须携带，
            // 否则正常时序（started 先到）下该提示被静默丢弃；乱序时（finished 先到、
            // started 后到）started 投影不含 notice（undefined），须跳过以免覆盖既有值。
            ...(tool.notice !== undefined ? { notice: tool.notice } : {}),
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
      event.event_type === "run_cancelled" ||
      event.event_type === "client_disconnected"
    ) {
      entries = entries.concat({
        kind: "status",
        eventId: event.event_id,
        eventType: event.event_type,
        payload: event.payload,
      });
    }

    if (event.event_type === "final_response") {
      // 终态回复到达即标记权威定稿，使后续迟到 delta 被丢弃（见 model_output_delta 分支），
      // 防止重排场景下与 final_response 文本重复展示。
      finalResponseReceived = true;
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
    finalResponseReceived,
    toolByCallId,
    delegationById,
    concurrencyByParentTurn,
    processedEventIds,
  };
}

/**
 * 读取「可见条目」：已定稿 entries + 仍在累积的 pending 块（按 turn 状态决定是否标记 streaming）。
 *
 * 目的:
 *   TimelineProjectorState.entries 只保存**已定稿**条目，pending 块不写入；
 *   否则每帧把未完成块 concat 进累积状态，会导致同一块被反复追加
 *   （"Hel" / "Hello" / "Hello world" 层层堆叠）。渲染所需的
 *   「定稿 + 进行中」视图在此按需派生，保证累积态干净且幂等。
 *
 * 契约:
 *   `isTurnActive` 仅控制 pending 块的 `streaming` 标记：
 *     - true（默认）= turn 仍在运行，pending 块以 streaming 推入；UI 持续展开 + caret。
 *     - false = turn 已进入终态（failed / cancelled / completed 等），即便 stateRef
 *       仍有 pending 残留（典型场景：SSE 客户端断开 → 后端不再投递终态 event 包，
 *       `flushPending` 永远不被触发；上一轮「思考到一半任务失败」bug 即源于此），
 *       也以 streaming 缺省推入；UI 按定稿态渲染，自然折叠回「深度思考 ▸」，
 *       不再与后续 turn 的渲染区重叠挤压空间。
 *   之所以放在此处而不是组件里：派生语义属于投影层契约，避免上层到处打补丁；
 *   默认 true 保持向后兼容，4 个既有调用点 + 4 个既有测试无需改动。
 *
 * 参数:
 *   state - 当前投影状态。
 *   isTurnActive - turn 是否仍处于运行/挂起态。默认 true。
 *
 * 返回:
 *   渲染用条目数组；无 pending 时直接返回 state.entries 原引用（零分配，引用稳定）。
 *
 * 异常:
 *   不抛出。
 *
 * @sideeffect 无（纯函数）。
 */
export function selectVisibleEntries(
  state: TimelineProjectorState,
  isTurnActive: boolean = true,
): TurnTimelineEntry[] {
  const { entries, pendingThinking, pendingDelta } = state;
  const hasThinking = Boolean(pendingThinking && pendingThinking.content.trim().length > 0);
  if (!hasThinking && !pendingDelta) {
    return entries;
  }
  // turn 已终态：pending 块一律按定稿态推入；accumulator 仍按原样保留，
  // 等待下一次增量投影时被新事件覆盖即可。
  const markStreaming = isTurnActive ? true : undefined;
  const visible = entries.slice();
  if (pendingThinking && hasThinking) {
    const thinkingEntry: TurnTimelineEntry = {
      kind: "thinking",
      eventId: pendingThinking.eventId,
      content: pendingThinking.content,
    };
    if (markStreaming !== undefined) {
      thinkingEntry.streaming = markStreaming;
    }
    visible.push(thinkingEntry);
  }
  if (pendingDelta) {
    const deltaEntry: TurnTimelineEntry = {
      kind: "assistant",
      eventId: pendingDelta.eventId,
      content: pendingDelta.content,
    };
    if (markStreaming !== undefined) {
      deltaEntry.streaming = markStreaming;
    }
    visible.push(deltaEntry);
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

/** Runtime event types that describe a delegation lifecycle transition. */
const DELEGATION_EVENTS = new Set<RuntimeEvent["event_type"]>([
  "delegation_started",
  "delegation_child_started",
  "delegation_finished",
  "delegation_failed",
  "delegation_cancelled",
]);

/**
 * Projects a delegation lifecycle event into the parent timeline item shape.
 *
 * @param event - Runtime event that may describe a delegation lifecycle transition.
 * @returns A delegation item for supported event types; otherwise null.
 *
 * @throws Does not throw; malformed optional fields are normalized or skipped.
 *
 * @sideeffect Emits a warning when a malformed delegation event cannot be safely keyed.
 */
function projectDelegation(event: RuntimeEvent): TimelineDelegationItem | null {
  if (!DELEGATION_EVENTS.has(event.event_type)) {
    return null;
  }
  const payload = event.payload as {
    delegation_id?: string;
    parent_turn_id?: string;
    child_turn_id?: string | null;
    child_agent_id?: string;
    delegation_type?: string;
    status?: unknown;
    summary?: string | null;
    error?: string | null;
  };
  const delegationId = typeof payload.delegation_id === "string" ? payload.delegation_id.trim() : "";
  if (!delegationId) {
    logWarn("delegation_event_missing_id", {
      module: "projector",
      event_id: event.event_id,
      event_type: event.event_type,
      task_id: event.task_id,
      turn_id: event.turn_id,
      reason: "delegation_id is required to merge lifecycle events safely",
    });
    return null;
  }
  return {
    eventId: event.event_id,
    delegationId,
    parentTurnId: String(payload.parent_turn_id ?? event.turn_id ?? ""),
    childTurnId: payload.child_turn_id ? String(payload.child_turn_id) : undefined,
    childAgentId: String(payload.child_agent_id ?? ""),
    delegationType: String(payload.delegation_type ?? ""),
    status: normalizeDelegationStatus(payload.status, event.event_type),
    summary: payload.summary ? String(payload.summary) : undefined,
    error: payload.error ? String(payload.error) : undefined,
  };
}

/**
 * Merges an incoming delegation event into an existing lifecycle item.
 *
 * @param existing - Previously projected delegation item.
 * @param incoming - Latest lifecycle event projection for the same delegation id.
 * @returns Merged item that preserves stable metadata and prevents nonterminal events from
 *   downgrading a terminal status.
 *
 * @throws Does not throw.
 *
 * @sideeffect None.
 */
function mergeDelegation(
  existing: TimelineDelegationItem,
  incoming: TimelineDelegationItem,
): TimelineDelegationItem {
  const keepTerminalStatus = isDelegationTerminal(existing.status) && !isDelegationTerminal(incoming.status);
  return {
    ...existing,
    ...incoming,
    childTurnId: incoming.childTurnId ?? existing.childTurnId,
    childAgentId: incoming.childAgentId || existing.childAgentId,
    delegationType: incoming.delegationType || existing.delegationType,
    status: keepTerminalStatus ? existing.status : incoming.status,
    summary: incoming.summary ?? existing.summary,
    error: incoming.error ?? existing.error,
  };
}

/**
 * 为 delegation 条目附加并发组字段（并发组规模与组内序号）。
 *
 * 逻辑：从 `concurrencyByParentTurn` 取该 delegation 所属 parent turn 当前 running 的
 * delegation 集合；仅当集合规模 >= 2（构成并发组）时，才在返回副本上附加
 * `concurrencyGroupSize`（集合规模）与 `concurrencyIndex`（该 delegation 在集合中的插入序，
 * 因 Set 迭代顺序即插入顺序故序号稳定）。集合规模 < 2 时不附加任何并发字段，保持 undefined。
 *
 * @param item - 待附加并发字段的 delegation 条目（不会被修改）。
 * @param concurrencyByParentTurn - parentTurnId → 当前 running 的 delegationId 集合映射。
 * @returns 新 delegation 条目：规模 >= 2 时带并发字段，否则沿用原条目的浅拷贝。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无（纯函数，不修改入参与传入的 Map/Set）。
 */
function attachConcurrency(
  item: TimelineDelegationItem,
  concurrencyByParentTurn: Map<string, Set<string>>,
): TimelineDelegationItem {
  const runningSet = concurrencyByParentTurn.get(item.parentTurnId);
  const groupSize = runningSet ? runningSet.size : 0;
  // 自身已终态（不再 running）或组规模 < 2：均不计入并发组，显式清除并发字段
  // （避免沿用上一次投影残留的陈旧值）。
  if (groupSize < 2 || isDelegationTerminal(item.status)) {
    return { ...item, concurrencyGroupSize: undefined, concurrencyIndex: undefined };
  }
  return {
    ...item,
    concurrencyGroupSize: groupSize,
    concurrencyIndex: [...runningSet].indexOf(item.delegationId),
  };
}

/**
 * 重算指定 parent turn 下所有 delegation 条目的并发字段。
 *
 * 用于解决「后到达的 sibling 改变并发组规模、但先到达条目已按旧规模定稿」的问题：
 * 每次 delegation 事件使某 parent 的 running 集合规模变化时，调用本函数把该 parent 下
 * 所有已投影 delegation 条目按最新集合重算并发字段。仅当某条目的并发字段（规模/序号）
 * 实际变化时才生成新引用，未变条目沿用旧引用以保 memo 稳定。
 *
 * @param entries - 当前显示条目数组（函数内部只读；仅在需要时返回新数组）。
 * @param parentTurnId - 集合发生变化的 parent turn 标识。
 * @param concurrencyByParentTurn - parentTurnId → 当前 running 的 delegationId 集合映射。
 * @returns 仍含最新并发字段的条目数组；若无任何条目变化则直接返回原 `entries` 引用。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无（纯函数，不修改入参的数组/对象/Map/Set）。
 */
function reattachConcurrencyForParent(
  entries: TurnTimelineEntry[],
  parentTurnId: string,
  concurrencyByParentTurn: Map<string, Set<string>>,
): TurnTimelineEntry[] {
  let changed = false;
  const next = entries.map((entry) => {
    if (entry.kind !== "delegation" || entry.item.parentTurnId !== parentTurnId) {
      return entry;
    }
    const updated = attachConcurrency(entry.item, concurrencyByParentTurn);
    if (
      updated.concurrencyGroupSize !== entry.item.concurrencyGroupSize ||
      updated.concurrencyIndex !== entry.item.concurrencyIndex
    ) {
      changed = true;
      return { kind: "delegation", item: updated } as TurnTimelineEntry;
    }
    return entry;
  });
  return changed ? next : entries;
}

/**
 * Converts a delegation event type into its fallback lifecycle status.
 *
 * @param eventType - Runtime event type.
 * @returns Delegation status implied by the event type.
 *
 * @throws Does not throw.
 *
 * @sideeffect None.
 */
function delegationStatusFromEvent(eventType: RuntimeEvent["event_type"]): TimelineDelegationStatus {
  switch (eventType) {
    case "delegation_child_started":
      return "running";
    case "delegation_finished":
      return "completed";
    case "delegation_failed":
      return "failed";
    case "delegation_cancelled":
      return "cancelled";
    default:
      return "pending";
  }
}

/**
 * Normalizes backend-provided delegation status into UI-supported status values.
 *
 * @param rawStatus - Raw payload status value.
 * @param eventType - Runtime event type used as the fallback source of truth.
 * @returns A supported timeline status.
 *
 * @throws Does not throw.
 *
 * @sideeffect None.
 */
function normalizeDelegationStatus(
  rawStatus: unknown,
  eventType: RuntimeEvent["event_type"],
): TimelineDelegationStatus {
  if (
    rawStatus === "pending" ||
    rawStatus === "running" ||
    rawStatus === "waiting_approval" ||
    rawStatus === "completed" ||
    rawStatus === "failed" ||
    rawStatus === "cancelled"
  ) {
    return rawStatus;
  }
  return delegationStatusFromEvent(eventType);
}

/**
 * Returns whether a delegation status is terminal.
 *
 * @param status - Delegation lifecycle status.
 * @returns True for completed, failed, and cancelled statuses.
 *
 * @throws Does not throw.
 *
 * @sideeffect None.
 */
function isDelegationTerminal(status: TimelineDelegationStatus): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

/**
 * 从扁平事件流中派生指定 child turn 的委派终态状态（供表现层复用，避免重复造轮子）。
 *
 * delegation 生命周期事件的 `turn_id` 属于父 turn，但其 `payload.child_turn_id`
 * 指向真实 child turn，故按 payload 反查。取 sequence 最大的有效事件作为当前状态，
 * 复用本文件既有的 `normalizeDelegationStatus`（payload/事件类型→状态归一）与
 * `delegationStatusFromEvent`（事件类型兜底推导），保证与投影器其它路径口径完全一致。
 *
 * @param childTurnId - 待查询的 child turn 标识。
 * @param events - 扁平事件流（来自 eventStore.events）。
 * @returns 派生状态（TimelineDelegationStatus 精确联合类型），或 undefined（尚无委派事件）。
 *   undefined 与 projector 兜底口径一致，由调用方降级处理（如徽章显示"状态未知"）。
 *
 * @throws 不抛出异常；payload 字段缺失或类型异常时安全跳过该事件。
 *
 * @sideeffect 无（纯函数，不修改入参）。
 */
export function deriveChildDelegationStatus(
  childTurnId: string,
  events: RuntimeEvent[],
): TimelineDelegationStatus | undefined {
  let best: { sequence: number; status: TimelineDelegationStatus } | undefined;
  for (const event of events) {
    // 仅关注 child 生命周期事件；delegation_started（父委派）无 child_turn_id，不会误匹配。
    if (
      event.event_type !== "delegation_child_started" &&
      event.event_type !== "delegation_finished" &&
      event.event_type !== "delegation_failed" &&
      event.event_type !== "delegation_cancelled"
    ) {
      continue;
    }
    const payload = event.payload as { child_turn_id?: string; status?: unknown };
    if (payload.child_turn_id !== childTurnId) continue;
    const status = normalizeDelegationStatus(payload.status, event.event_type);
    const sequence = Number(event.sequence || 0);
    if (!best || sequence >= best.sequence) {
      best = { sequence, status };
    }
  }
  return best?.status;
}

/** 同一并发组（同 parent turn）下的一个 sibling 子 Agent 派生视图。 */
export interface SiblingDelegation {
  /** sibling child turn 标识。 */
  childTurnId: string;
  /** sibling child AgentProfile id。 */
  childAgentId: string;
  /** 该 sibling 的委派生命周期状态（由最新事件归一）。 */
  status: TimelineDelegationStatus;
}

/**
 * 从扁平事件流中派生指定 child turn 的并发 sibling 列表（供右侧面板以 tab 列出并切换）。
 *
 * 逻辑（纯前端推导，零新事件、零后端改动）：
 * 1. 在 `events` 中找 `delegation_child_started` 事件且 `child_turn_id === selectedChildTurnId`，
 *    取其 `parent_turn_id`（记为 parentTurnId）；找不到（无选中对、或选中项不是并发 child）返回 `[]`。
 * 2. 遍历 `events` 中 `DELEGATION_EVENTS` 内、`parent_turn_id === parentTurnId` 的事件，
 *    按 `delegation_id` 分组；每组取最新状态（复用 `normalizeDelegationStatus` /
 *    `delegationStatusFromEvent` 口径，不平行重写）与最新 `child_turn_id` / `child_agent_id`，
 *    构造 `SiblingDelegation[]`。
 * 3. 返回列表（长度不限）；调用方（SubagentPanel）只在长度 >= 2 时渲染并发 tab。
 * 注意：同一 delegation_id 的不同事件按 sequence 取最新，保证与投影器其它路径状态口径一致。
 *
 * @param events - 扁平事件流（来自 eventStore.events）。
 * @param selectedChildTurnId - 当前选中的 child turn 标识。
 * @returns sibling 派生视图数组；无并发关系时返回空数组 `[]`。
 *
 * @throws 不抛出异常；payload 字段缺失或类型异常时安全跳过该事件。
 *
 * @sideeffect 无（纯函数，不修改入参）。
 */
export function deriveSiblingDelegations(
  events: RuntimeEvent[],
  selectedChildTurnId: string,
): SiblingDelegation[] {
  if (!selectedChildTurnId) return [];

  // 1. 反查选中 child 的 parent turn（仅 delegation_child_started 带 child_turn_id）。
  let parentTurnId: string | undefined;
  for (const event of events) {
    if (event.event_type !== "delegation_child_started") continue;
    const payload = event.payload as { child_turn_id?: string; parent_turn_id?: string };
    if (payload.child_turn_id !== selectedChildTurnId) continue;
    parentTurnId = payload.parent_turn_id ?? event.turn_id;
    break;
  }
  if (parentTurnId == null) return [];

  // 2. 按 delegation_id 分组，取每组最新状态与最新 child/agent 标识。
  const byDelegation = new Map<
    string,
    { sequence: number; status: TimelineDelegationStatus; childTurnId?: string; childAgentId: string }
  >();
  for (const event of events) {
    if (!DELEGATION_EVENTS.has(event.event_type)) continue;
    const payload = event.payload as {
      delegation_id?: string;
      parent_turn_id?: string;
      child_turn_id?: string | null;
      child_agent_id?: string;
      status?: unknown;
    };
    if (payload.parent_turn_id !== parentTurnId) continue;
    const delegationId = typeof payload.delegation_id === "string" ? payload.delegation_id.trim() : "";
    if (!delegationId) continue;
    const sequence = Number(event.sequence || 0);
    const existing = byDelegation.get(delegationId);
    if (existing && sequence < existing.sequence) continue;
    byDelegation.set(delegationId, {
      sequence,
      status: normalizeDelegationStatus(payload.status, event.event_type),
      childTurnId: payload.child_turn_id ? String(payload.child_turn_id) : existing?.childTurnId,
      childAgentId: String(payload.child_agent_id ?? existing?.childAgentId ?? ""),
    });
  }

  // 3. 构造派生视图（仅保留有 child_turn_id 的 sibling）。
  const siblings: SiblingDelegation[] = [];
  for (const entry of byDelegation.values()) {
    if (!entry.childTurnId) continue;
    siblings.push({
      childTurnId: entry.childTurnId,
      childAgentId: entry.childAgentId,
      status: entry.status,
    });
  }
  return siblings;
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
