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
  setEvents: (events: RuntimeEvent[]) => void;

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
  connectionState: SSEConnectionState.IDLE,
  processedEventIds: new Set<string>(),

  // --- 动作 ---

  appendEvent: (event: RuntimeEvent) => {
    set((state) => {
      // 回放去重：已处理过的事件直接跳过
      if (state.processedEventIds.has(event.event_id)) {
        return state;
      }

      return {
        events: [...state.events, event],
        processedEventIds: new Set([...state.processedEventIds, event.event_id]),
      };
    });
  },

  setEvents: (events: RuntimeEvent[]) => {
    const ids = new Set(events.map((e) => e.event_id));
    set({
      events,
      processedEventIds: ids,
    });
  },

  setConnectionState: (state: SSEConnectionState) => {
    set({ connectionState: state });
  },

  clearEvents: () => {
    set({
      events: [],
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
