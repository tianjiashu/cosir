/**
 * 运行时事件流状态管理（Zustand）。
 *
 * 管理：
 * - 当前任务的事件流数组
 * - SSE 连接状态
 * - 增量事件追加（含回放去重/幂等处理）
 *
 * 后端 `run_task` 在任务非 pending 状态时会先回放已持久化事件，
 * 前端通过 event_id 去重避免重复渲染历史步骤。
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
  /** 按 task_id 聚合的事件事实。 */
  eventsByTaskId: Record<string, RuntimeEvent[]>;
  /** 按 turn_id 聚合的事件事实。 */
  eventsByTurnId: Record<string, RuntimeEvent[]>;
  /** SSE 连接的当前状态。 */
  connectionState: SSEConnectionState;
  /** 已处理的 event_id 集合（用于回放去重）。 */
  processedEventIds: Set<string>;
}

/** 事件 Store 的动作接口。 */
interface EventActions {
  /**
   * 追加新事件到流中。
   *
   * 通过 event_id 去重：如果 event_id 已存在于 processedEventIds 中，
   * 则跳过该事件（回放幂等）。否则追加到数组末尾并记录 ID。
   *
   * @param event - 待追加的运行时事件。
   */
  appendEvent: (event: RuntimeEvent) => void;

  /** 批量设置事件列表（用于从 API 加载历史事件时替换整个列表）。 */
  setEvents: (events: RuntimeEvent[], taskId?: string) => void;

  /** 更新 SSE 连接状态。 */
  setConnectionState: (state: SSEConnectionState) => void;

  /** 清空当前事件流和去重集合（切换任务时调用）。 */
  clearEvents: () => void;
}

/**
 * 事件 Zustand Store 实例。
 *
 * 核心设计：appendEvent 内置基于 event_id 的去重逻辑，
 * 对齐后端 runner.py 的回放语义——当任务非 pending 时
 * 先 replay 已持久化事件，前端必须幂等处理。
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
      // 回放去重：已处理过的事件直接跳过
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
 * @returns 指定任务下按 sequence 排序的事件列表。
 */
export function selectEventsForTask(state: EventState, taskId: string | null): RuntimeEvent[] {
  if (!taskId) return [];
  return state.eventsByTaskId[taskId] ?? [];
}

/**
 * 按后端稳定 sequence 排序运行时事件。
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
