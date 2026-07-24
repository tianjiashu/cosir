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
   * @param event - 待追加的运行时事件。
   */
  appendEvent: (event: RuntimeEvent) => void;

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
        return state;
      }

      const events = [...state.events, event].sort(compareRuntimeEvents);
      const taskEvents = [...(state.eventsByTaskId[event.task_id] ?? []), event].sort(compareRuntimeEvents);
      const turnEvents = event.turn_id
        ? [...(state.eventsByTurnId[event.turn_id] ?? []), event].sort(compareRuntimeEvents)
        : [];
      return {
        events,
        eventsByTaskId: { ...state.eventsByTaskId, [event.task_id]: taskEvents },
        eventsByTurnId: event.turn_id
          ? { ...state.eventsByTurnId, [event.turn_id]: turnEvents }
          : state.eventsByTurnId,
        processedEventIds: new Set([...state.processedEventIds, event.event_id]),
      };
    });
  },

  setEvents: (events: RuntimeEvent[], taskId?: string) => {
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

      // 去重集合取并集（历史 + 实时 SSE 共同去重）
      const mergedIds = new Set([...state.processedEventIds, ...incomingIds]);

      return {
        events: sorted,
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
 * 获取当前事件流的最新一条事件。
 * @returns 最新事件或 undefined。
 */
export const selectLatestEvent = (state: EventState): RuntimeEvent | undefined => {
  return state.events[state.events.length - 1];
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
  if (!taskId) return [];
  return state.eventsByTaskId[taskId] ?? [];
}

/**
 * 按 event_id 合并两组事件（后者优先），用于 setEvents 跨任务 / 轮次合并历史缓存。
 *
 * @param target - 已有的事件列表（可为 undefined）。
 * @param incoming - 新拉取 / 传入的事件列表。
 * @returns 去重并按客户端顺序排序后的合并事件列表。
 */
function mergeByEventId(
  target: RuntimeEvent[] | undefined,
  incoming: RuntimeEvent[],
): RuntimeEvent[] {
  const map = new Map<string, RuntimeEvent>();
  for (const e of target ?? []) {
    map.set(e.event_id, e);
  }
  // 传入事件优先（重新打开任务时后端回放为最新全量）
  for (const e of incoming) {
    map.set(e.event_id, e);
  }
  return [...map.values()].sort(compareRuntimeEvents);
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

