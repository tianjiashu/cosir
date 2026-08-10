/**
 * 运行时事件流状态管理（Zustand）。
 *
 * 管理：
 * - 当前任务的事件流数组
 * - SSE 连接状态
 * - 增量事件追加（含重复事件幂等处理）
 *
 * 后端已持久化并支持 runtime events 回放（GET /tasks/{task_id}/events 等）。
 * 打开任务时由 useTask.openTask 拉取历史事件经 setEvents 灌入本 store；本 store
 * 同时承担「跨任务切换的内存缓存」职责：按 task_id / turn_id 分组保留历史，
 * 再次打开同一任务时无需重复请求（历史对话不可变），实时 SSE 事件经 appendEvent
 * 增量合并（event_id 去重）。
 *
 * @module stores/eventStore
 */

import { create } from "zustand";
import type { RuntimeEvent } from "@shared/events";
import { SSEConnectionState } from "../services/sse";
import { logWarn } from "../lib/logger";
import { PerfTrace } from "../lib/perf";

/**
 * 模块级空事件常量，复用同一引用，避免每次 `?? []` 产生新数组字面量
 * 导致 useShallow 在任务无缓存时恒定判定为“变化”而触发多余重渲染。
 */
export const EMPTY_EVENTS: RuntimeEvent[] = [];

/** 事件 Store 的状态接口。 */
interface EventState {
  /** 当前正在查看的任务事件流（按时间排序）。 */
  events: RuntimeEvent[];
  /** 按 task_id 聚合的客户端内存事件。 */
  eventsByTaskId: Record<string, RuntimeEvent[]>;
  /** 按 turn_id 聚合的客户端内存事件。 */
  eventsByTurnId: Record<string, RuntimeEvent[]>;
  /** SSE 连接的当前状态。 */
  connectionState: SSEConnectionState;
  /** 已处理的 event_id 集合（用于重复事件去重）。 */
  processedEventIds: Set<string>;
}

/** 事件 Store 的动作接口。 */
interface EventActions {
  /**
   * 追加新事件到流中。
   *
   * 通过 event_id 去重：如果 event_id 已存在于 processedEventIds 中，
   * 则跳过该事件。否则追加到数组末尾并记录 ID。
   *
   * 性能约定：后端 sequence 全局单调，实时事件天然落在各分片数组末尾，
   * 故直接 push（O(1) 摊还），不再对每个事件做三次全量 O(n log n) 排序，
   * 避免高频 delta 下大量比较拖慢主线程。
   *
   * @param event - 待追加的运行时事件。
   */
  appendEvent: (event: RuntimeEvent) => void;

  /**
   * 批量追加事件（单次 set，单次渲染）。
   *
   * 用于 SSE 消费层把同一动画帧内的多个 delta 攒批后一次性提交，
   * 将「每 delta 一次 set + 投影 + 重渲染」降为「每帧一次」，
   * 在不丢失实时性的前提下显著削减高频流式下的渲染压力。
   *
   * @param incoming - 待追加的事件列表（内部仍按 event_id 去重）。
   */
  appendEvents: (incoming: RuntimeEvent[]) => void;

  /** 批量设置事件列表（用于切换任务或重置当前客户端事件缓存）。 */
  setEvents: (events: RuntimeEvent[], taskId?: string) => void;


  /** 更新 SSE 连接状态。 */
  setConnectionState: (state: SSEConnectionState) => void;

  /** 清空当前事件流和去重集合（切换任务时调用）。 */
  clearEvents: () => void;

  /** 使指定任务的历史事件缓存失效（删除任务 / 工作区时调用）。 */
  invalidateTask: (taskId: string) => void;
}

/**
 * 事件 Zustand Store 实例。
 *
 * 核心设计：appendEvent 内置基于 event_id 的去重逻辑；setEvents 以合并方式写入，
 * 保留其他任务 / 轮次的历史缓存（跨任务切换不重复拉取），实时 SSE 与历史回放经
 * event_id 去重合并。
 */
export const useEventStore = create<EventState & EventActions>((set) => ({
  // --- 初始状态 ---
  events: [],
  eventsByTaskId: {},
  eventsByTurnId: {},
  connectionState: SSEConnectionState.IDLE,
  processedEventIds: new Set<string>(),

  // --- 动作 ---

  appendEvent: (event: RuntimeEvent) => {
    set((state) => {
      // 重复事件去重：已处理过的事件直接跳过
      if (state.processedEventIds.has(event.event_id)) {
        // 调试：命中去重说明同一 event_id 被重复投递（实时流 + 历史回放等跨通道），
        // 用于排查"事件被重复处理"导致思考块内容翻倍/异常。
        logWarn("event_dup_skipped", {
          module: "eventStore",
          event_id: event.event_id,
          event_type: event.event_type,
          turn_id: event.turn_id,
          task_id: event.task_id,
        });
        return state;
      }

      // 扁平 events 与两个分片共用 appendOrderedShard，保证三者顺序口径完全同源，
      // 乱序在 store 层即归位（详见 appendOrderedShard 的契约及其对 TurnTimeline 续算的影响）。
      const events = appendOrderedShard(state.events, event);
      const eventsByTaskId = {
        ...state.eventsByTaskId,
        [event.task_id]: appendOrderedShard(state.eventsByTaskId[event.task_id], event),
      };
      let eventsByTurnId = state.eventsByTurnId;
      if (event.turn_id) {
        eventsByTurnId = {
          ...state.eventsByTurnId,
          [event.turn_id]: appendOrderedShard(state.eventsByTurnId[event.turn_id], event),
        };
      }
      const processedEventIds = new Set(state.processedEventIds);
      processedEventIds.add(event.event_id);
      return { events, eventsByTaskId, eventsByTurnId, processedEventIds };
    });
  },

  appendEvents: (incoming: RuntimeEvent[]) => {
    if (incoming.length === 0) {
      return;
    }
    set((state) => {
      PerfTrace.markCurrent("eventStore:appendEvents-batch-start", {
        incoming: incoming.length,
        task_id: incoming[0]?.task_id,
        turn_id: incoming[0]?.turn_id,
      });
      const eventsByTaskId = { ...state.eventsByTaskId };
      const eventsByTurnId = { ...state.eventsByTurnId };
      const processedEventIds = new Set(state.processedEventIds);
      // 先按 event_id 去重，避免攒批内重复（如实时流与历史回放叠加）污染扁平数组与分片。
      const deduped: RuntimeEvent[] = [];
      for (const event of incoming) {
        if (processedEventIds.has(event.event_id)) {
          // 攒批内或跨通道重复，跳过（保持与 appendEvent 一致的去重语义）
          logWarn("event_dup_skipped", {
            module: "eventStore",
            event_id: event.event_id,
            event_type: event.event_type,
            turn_id: event.turn_id,
            task_id: event.task_id,
          });
          continue;
        }
        processedEventIds.add(event.event_id);
        deduped.push(event);
        // 分片逐条经 appendOrderedShard 追加（口径同 appendEvent，见其 JSDoc）。
        eventsByTaskId[event.task_id] = appendOrderedShard(eventsByTaskId[event.task_id], event);
        if (event.turn_id) {
          eventsByTurnId[event.turn_id] = appendOrderedShard(eventsByTurnId[event.turn_id], event);
        }
      }
      if (deduped.length === 0) {
        return state;
      }
      // 扁平 events 同样逐条经 appendOrderedShard 追加：批内乱序（如网络重排的
      // [seq=2, seq=1]）在逐条比对末位时即被归位，结果等价于旧「整批 sort」，且与
      // 分片、appendEvent 共用同一函数，三处顺序口径单一事实来源，杜绝分歧。
      let events = state.events;
      for (const event of deduped) {
        events = appendOrderedShard(events, event);
      }
      const targetTaskId = deduped[0]?.task_id;
      const taskTotal = targetTaskId ? eventsByTaskId[targetTaskId]?.length ?? 0 : 0;
      PerfTrace.markCurrent("eventStore:appendEvents-batch-committed", {
        accepted: deduped.length,
        task_total_events: taskTotal,
        turn_id: deduped[0]?.turn_id,
      });
      return { events, eventsByTaskId, eventsByTurnId, processedEventIds };
    });
  },

  setEvents: (events: RuntimeEvent[], taskId?: string) => {
    if (events.length === 0 && taskId == null) {
      // 空输入且无任务维度时无需任何合并：短路返回，避免无意义的全量重排与引用失效
      // （与 appendEvents 空输入短路一致，保护下游 memo 跳过重渲染）。
      return;
    }
    set((state) => {
      const sorted = [...events].sort(compareRuntimeEvents);
      const incomingIds = new Set(events.map((e) => e.event_id));

      // 合并 eventsByTaskId：保留其他任务缓存，当前任务用传入并集去重覆盖
      const mergedByTask = { ...state.eventsByTaskId };
      if (taskId) {
        mergedByTask[taskId] = mergeByEventId(state.eventsByTaskId[taskId], sorted);
      } else {
        for (const e of sorted) {
          mergedByTask[e.task_id] = mergeByEventId(mergedByTask[e.task_id], [e]);
        }
      }

      // 合并 eventsByTurnId：保留其他轮次缓存
      const mergedByTurn = { ...state.eventsByTurnId };
      for (const e of sorted) {
        if (e.turn_id) {
          mergedByTurn[e.turn_id] = mergeByEventId(mergedByTurn[e.turn_id], [e]);
        }
      }

      // 扁平 `events` 必须与分片视图保持一致。根因教训：此前用「排除同 task + concat
      // 传入集」实现全量覆盖，但分片（上面 mergedByTask）走 mergeByEventId **合并保留**
      // 同 task 旧事件——两条口径矛盾。当调用方以增量方式 setEvents（只传新增子集，
      // 而非该 task 全量历史）时，扁平数组会丢弃同 task 之前缓存的事件，导致
      // eventsByTaskId 101 条 / events 只剩 1 条的自相矛盾，且 selectEventCount 少算。
      // 修复：把**完整**扁平数组作为 mergeByEventId 的 target（不排除同 task），
      // sorted 作为 incoming——merge 天然按 event_id 去重合并：incoming 覆盖同 task
      // 旧事件、追加新增、保留不同 task。这与分片 mergedByTask / mergedByTurn 走同一
      // mergeByEventId，去重与引用稳定语义完全一致。
      const mergedEvents = mergeByEventId(state.events, sorted).sort(compareRuntimeEvents);

      // 去重集合取并集（历史 + 实时 SSE 共同去重）
      const mergedIds = new Set([...state.processedEventIds, ...incomingIds]);

      return {
        events: mergedEvents,
        eventsByTaskId: mergedByTask,
        eventsByTurnId: mergedByTurn,
        processedEventIds: mergedIds,
      };
    });
  },

  setConnectionState: (state: SSEConnectionState) => {
    set({ connectionState: state });
  },

  clearEvents: () => {
    set({
      events: [],
      eventsByTaskId: {},
      eventsByTurnId: {},
      processedEventIds: new Set(),
      connectionState: SSEConnectionState.IDLE,
    });
  },

  invalidateTask: (taskId: string) => {
    set((state) => {
      const removedEvents = state.eventsByTaskId[taskId] ?? [];
      if (removedEvents.length === 0) {
        return state;
      }
      const removedTurnIds = new Set(
        removedEvents.map((e) => e.turn_id).filter((id): id is string => Boolean(id)),
      );
      const removedIds = new Set(removedEvents.map((e) => e.event_id));
      const eventsByTaskId = { ...state.eventsByTaskId };
      delete eventsByTaskId[taskId];
      const eventsByTurnId = { ...state.eventsByTurnId };
      for (const turnId of removedTurnIds) {
        delete eventsByTurnId[turnId];
      }
      return {
        events: state.events.filter((e) => e.task_id !== taskId),
        eventsByTaskId,
        eventsByTurnId,
        processedEventIds: new Set(
          [...state.processedEventIds].filter((id) => !removedIds.has(id)),
        ),
      };
    });
  },
}));

// ---------- 派生选择器 ----------

/**
 * 获取指定任务事件流的最新一条事件（按当前客户端缓存顺序）。
 *
 * 严格按 taskId 隔离，避免后台其它任务的 SSE 事件污染当前视图的滚动与派生状态。
 *
 * @param state - 事件 store 状态。
 * @param taskId - 任务标识；为空或非字符串时返回 undefined。
 * @returns 该任务下最新事件，或 undefined。
 */
export const selectLatestEvent = (state: EventState, taskId: string | null): RuntimeEvent | undefined => {
  if (!taskId || typeof taskId !== "string") return undefined;
  const taskEvents = state.eventsByTaskId[taskId];
  if (!taskEvents || taskEvents.length === 0) return undefined;
  return taskEvents[taskEvents.length - 1];
};

/**
 * 获取当前事件总数。
 * @returns 事件数量。
 */
export const selectEventCount = (state: EventState): number => {
  return state.events.length;
};

/**
 * 读取指定任务的事件。
 *
 * @param state - 事件 store 状态。
 * @param taskId - 任务标识。
 * @returns 指定任务下按客户端缓存顺序排序的事件列表。
 */
export function selectEventsForTask(state: EventState, taskId: string | null): RuntimeEvent[] {
  if (!taskId) return EMPTY_EVENTS;
  return state.eventsByTaskId[taskId] ?? EMPTY_EVENTS;
}

/**
 * 按 event_id 合并两组事件（后者优先），用于 setEvents 跨任务 / 轮次合并历史缓存。
 *
 * 引用稳定性（性能关键）：当合并结果与 `target` 逐项引用相同时，直接复用 `target` 的数组引用，
 * 而非返回新数组。setEvents 合并历史时会遍历全部传入事件并刷新 `eventsByTurnId` / `eventsByTaskId`
 * 的切分映射——若每次都无条件生成新数组引用，会让「未变化的轮次 / 任务」的事件列表引用失效，
 * 进而击穿下游 TurnTimeline 的 memo（依赖 events 引用跳过重渲染），导致「加载历史对话时整棵
 * timeline 全量重投影 + 重渲染 markdown」卡顿。复用旧引用可让未变化轮次精确跳过。
 *
 * 现实边界：冷启动路径（openTask 经 `api.listTaskEvents` 拉取）返回的是新反序列化对象，
 * 引用必与旧缓存不同，此时本函数仍会返回新数组；该路径下 events 引用稳定的真正来源是
 * `useTask.openTask` 的「内存缓存命中即跳过 setEvents」优化。本函数的引用复用主要作为
 * 「同引用幂等合并」的语义防线（如重复传入同一批已缓存对象、或实时流与回放叠加的合并），
 * 与 openTask 缓存命中共同构成引用稳定保障。
 *
 * @param target - 已有的事件列表（可为 undefined）。
 * @param incoming - 新拉取 / 传入的事件列表。
 * @returns 去重并按客户端顺序排序后的合并事件列表；若与 target 等价则复用 target 引用。
 */
function mergeByEventId(
  target: RuntimeEvent[] | undefined,
  incoming: RuntimeEvent[],
): RuntimeEvent[] {
  const targetArr = target ?? [];
  const map = new Map<string, RuntimeEvent>();
  for (const e of targetArr) {
    map.set(e.event_id, e);
  }
  // 传入事件优先（重新打开任务时后端回放为最新全量）；仅当某 event_id 对应的事件
  // 引用发生变化（新增或内容更新）时才标记 changed。
  let changed = false;
  for (const e of incoming) {
    if (map.get(e.event_id) !== e) {
      changed = true;
      map.set(e.event_id, e);
    }
  }
  if (!changed) {
    // 合并结果与 target 完全等价，复用旧引用以保住下游 memo 跳过。
    return targetArr;
  }
  return [...map.values()].sort(compareRuntimeEvents);
}

/**
 * 向有序分片追加单条事件，保持整体按 sequence 有序。
 *
 * 性能约定：后端 sequence 通常全局单调，实时事件天然落在分片末尾，
 * 故正常顺序到达时直接 concat（O(1) 摊还）；仅当新事件相对分片末位「逆序」
 * （compareRuntimeEvents 全键判定，含 sequence 相等时的 created_at 次级比较，
 * 与排序口径完全一致）时才整体排序，避免高频 delta 下每事件全量 O(n log n)。
 *
 * 该逻辑在扁平 events 与两个分片（eventsByTaskId / eventsByTurnId）上口径一致，
 * 确保「渲染实际消费的分片」与「扁平数组」顺序同源，乱序在 store 层即被归位，
 * 不再传导到下游 TurnTimeline 的尾部续算（其假设 append-only 且有序）。
 *
 * @param shard - 既有的有序分片（可为 undefined，表示首个事件）。
 * @param event - 待追加的事件。
 * @returns 追加并保持有序后的新分片数组。
 */
function appendOrderedShard(
  shard: RuntimeEvent[] | undefined,
  event: RuntimeEvent,
): RuntimeEvent[] {
  if (!shard || shard.length === 0) {
    return [event];
  }
  const lastEvent = shard[shard.length - 1];
  const needsReorder = compareRuntimeEvents(event, lastEvent) < 0;
  return needsReorder ? shard.concat(event).sort(compareRuntimeEvents) : shard.concat(event);
}

/**
 * 按当前事件的 sequence + created_at 做客户端排序。
 *
 * @param left - 左侧事件。
 * @param right - 右侧事件。
 * @returns 负数表示 left 在前，正数表示 right 在前。
 */
function compareRuntimeEvents(left: RuntimeEvent, right: RuntimeEvent): number {
  const leftSequence = Number(left.sequence || 0);
  const rightSequence = Number(right.sequence || 0);
  if (leftSequence !== rightSequence) {
    return leftSequence - rightSequence;
  }
  return left.created_at.localeCompare(right.created_at);
}

