/**
 * 上下文窗口占用状态管理（Zustand）。
 *
 * 管理：当前任务上下文窗口的输入侧 token 占用（来自后端 CONTEXT_USAGE 事件）。
 * 该值基于 RuntimeContext.messages 本地估算，不依赖模型 usage_metadata，因此 turn
 * 中途取消也会因消息已在上下文中而保留最近一次占用，前端圆环不会因取消而失真。
 *
 * 与 eventStore 解耦：上下文占用是「最新值覆盖」语义（非事件历史），单独成 store
 * 避免 InputBar 订阅整个事件流造成不必要重渲染。
 *
 * @module stores/contextUsageStore
 */

import { create } from "zustand";
import type { ContextUsagePayload } from "@shared/events";

/** 上下文占用 Store 的状态接口。 */
interface ContextUsageState {
  /** 当前已用 token（输入侧估算）。 */
  usedTokens: number;
  /** 实际上下文窗口上限（token）= min(模型最大窗口, 全局软上限)。 */
  totalTokens: number;
  /** 最近一次更新的事件时间，用于排查与排序。 */
  updatedAt: string | null;
}

/** 上下文占用 Store 的动作接口。 */
interface ContextUsageActions {
  /** 用后端 CONTEXT_USAGE 事件的最新占用覆盖状态。 */
  setUsage: (payload: ContextUsagePayload, updatedAt: string) => void;
  /** 重置（如切换任务/工作区时清除旧占用）。 */
  reset: () => void;
}

export const useContextUsageStore = create<ContextUsageState & ContextUsageActions>((set) => ({
  usedTokens: 0,
  totalTokens: 0,
  updatedAt: null,
  setUsage: (payload, updatedAt) =>
    set({ usedTokens: payload.used_tokens, totalTokens: payload.total_tokens, updatedAt }),
  reset: () => set({ usedTokens: 0, totalTokens: 0, updatedAt: null }),
}));
