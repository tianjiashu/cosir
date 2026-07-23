/**
 * 运行时事件流状态管理（Zustand）。
 *
 * 管理：
 * - 当前任务的事件流数组
 * - SSE 连接状态
 * - 增量事件追加（含重复事件幂等处理）
 *
 * 当前后端不持久化、不回放 runtime events。打开历史任务时，
 * 可见对话由 turn 历史恢复；本 store 只缓存当前客户端会话内收到的 SSE 事件。
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
}

/**
 * 事件 Zustand Store 实例。
 *
 * 核心设计：appendEvent 内置基于 event_id 的去重逻辑。
 * 这只保证当前客户端会话内的重复事件不会重复渲染，不代表后端已经提供事件回放。
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
    const ids = new Set(events.map((e) => e.event_id));
    const sortedEvents = [...events].sort(compareRuntimeEvents);
    const nextByTurn = sortedEvents.reduce<Record<string, RuntimeEvent[]>>((acc, event) => {
      if (event.turn_id) {
        acc[event.turn_id] = [...(acc[event.turn_id] ?? []), event];
      }
      return acc;
    }, {});
    set({
      events: sortedEvents,
      eventsByTaskId: taskId ? { [taskId]: sortedEvents } : groupEventsByTask(sortedEvents),
      eventsByTurnId: nextByTurn,
      processedEventIds: ids,
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

/**
 * 按 task_id 聚合事件列表。
 *
 * @param events - 已排序或未排序的运行时事件。
 * @returns 以 task_id 为 key 的事件数组映射。
 */
function groupEventsByTask(events: RuntimeEvent[]): Record<string, RuntimeEvent[]> {
  return events.reduce<Record<string, RuntimeEvent[]>>((acc, event) => {
    acc[event.task_id] = [...(acc[event.task_id] ?? []), event].sort(compareRuntimeEvents);
    return acc;
  }, {});
}
