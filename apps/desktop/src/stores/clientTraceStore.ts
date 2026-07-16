/**
 * 客户端 trace 上下文 Store。
 *
 * 保存当前前端用户操作的 trace_id，供 API/SSE header 注入与前端日志隐式复用。
 *
 * @module stores/clientTraceStore
 */

import { create } from "zustand";
import type { BackendTraceHeaders, ClientTraceContext } from "@shared/tracePropagation";

/** 客户端 trace store 状态。 */
interface ClientTraceState {
  /** 当前活跃前端用户操作 trace。 */
  currentTrace: ClientTraceContext | null;
  /** 最近一次后端确认的 trace。 */
  lastTrace: BackendTraceHeaders | null;
}

/** 客户端 trace store 动作。 */
interface ClientTraceActions {
  /** 设置当前活跃 trace。 */
  setCurrentTrace: (trace: ClientTraceContext) => void;
  /** 清空当前活跃 trace。 */
  clearCurrentTrace: () => void;
  /** 记录后端响应确认的 trace。 */
  recordBackendTrace: (headers: BackendTraceHeaders) => void;
  /** 清空 store，主要用于测试。 */
  reset: () => void;
}

/**
 * 客户端 trace Zustand Store。
 *
 * 只保存状态和纯动作，不直接发起 HTTP、SSE 或 Tauri IPC 调用。
 */
export const useClientTraceStore = create<ClientTraceState & ClientTraceActions>((set) => ({
  currentTrace: null,
  lastTrace: null,

  setCurrentTrace: (trace) => {
    set({ currentTrace: trace, lastTrace: null });
  },

  clearCurrentTrace: () => {
    set({ currentTrace: null });
  },

  recordBackendTrace: (headers) => {
    set({ lastTrace: headers });
  },

  reset: () => {
    set({ currentTrace: null, lastTrace: null });
  },
}));

/**
 * 返回当前日志可用的客户端 trace 上下文字段。
 *
 * @returns 包含 trace_id/task_id/run_id 的扁平对象。
 */
export function selectClientLogContext(): Record<string, string> {
  const state = useClientTraceStore.getState();
  const context: Record<string, string> = {};
  if (state.currentTrace?.traceId) {
    context.trace_id = state.currentTrace.traceId;
  } else if (state.lastTrace?.traceId) {
    context.trace_id = state.lastTrace.traceId;
  }
  if (state.currentTrace?.taskId) {
    context.task_id = state.currentTrace.taskId;
  }
  if (state.currentTrace?.runId) {
    context.run_id = state.currentTrace.runId;
  }
  return context;
}
