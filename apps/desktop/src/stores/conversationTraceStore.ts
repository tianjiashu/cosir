/**
 * 对话相关请求 trace 状态。
 *
 * 记录客户端发起的任务、SSE、审批、恢复等对话链路请求所使用的 trace_id，
 * 供日志页和右侧诊断面板展示与查询。
 *
 * @module stores/conversationTraceStore
 */

import { create } from "zustand";

/**
 * 对话相关请求类型。
 *
 * 表示客户端在一次对话生命周期中会发起的 HTTP/SSE 请求类别。
 * 这些值只用于前端诊断展示和日志查询入口选择，不参与后端协议持久化。
 * 类型本身没有运行时副作用；未知端点应先扩展该联合类型再记录。
 */
export type ConversationTraceOperation =
  | "task_create"
  | "task_get"
  | "task_events"
  | "task_checkpoints"
  | "task_cancel"
  | "task_stream"
  | "task_approvals"
  | "approval_decision"
  | "recoverable_runs";

/**
 * 一次对话相关请求使用的 trace 记录。
 *
 * 记录由 tracePropagation 生成并注入请求头的 trace_id，以及它所属的
 * task、approval、HTTP 方法和路径。该记录是前端内存诊断状态，不保证应用
 * 重启后仍可恢复；后端持久 trace 绑定应作为单独能力实现。
 */
export interface ConversationTraceRecord {
  /** 客户端请求使用的 trace 标识。 */
  traceId: string;
  /** 请求所属任务；全局请求或无法归属任务时为空。 */
  taskId: string;
  /** 关联审批标识；非审批请求时为空。 */
  approvalId: string;
  /** 对话请求类型。 */
  operation: ConversationTraceOperation;
  /** HTTP 方法。 */
  method: string;
  /** 后端 API 路径。 */
  path: string;
  /** 前端记录该 trace 的时间。 */
  recordedAt: string;
}

/** Trace store 状态。 */
interface ConversationTraceState {
  /** 所有已记录的对话请求 trace，按记录顺序追加。 */
  traces: ConversationTraceRecord[];
  /** 最近一次对话相关请求 trace。 */
  latestTrace: ConversationTraceRecord | null;
  /** 按 task_id 索引的最近 trace。 */
  latestTraceByTaskId: Record<string, ConversationTraceRecord>;
  /** 按 task_id 索引的 SSE stream trace。 */
  streamTraceByTaskId: Record<string, ConversationTraceRecord>;
  /** 按 task_id 和 operation 索引的最近 trace。 */
  latestTraceByTaskIdAndOperation: Record<string, Partial<Record<ConversationTraceOperation, ConversationTraceRecord>>>;
}

/** Trace store 动作。 */
interface ConversationTraceActions {
  /** 记录一次对话相关请求 trace。 */
  recordTrace: (record: Omit<ConversationTraceRecord, "recordedAt">) => void;
  /** 清空全部 trace 状态，主要用于测试。 */
  resetConversationTraces: () => void;
}

/**
 * 对话 trace Zustand Store。
 *
 * 只保存内存状态，不发起 HTTP/SSE/IPC，不写日志。
 */
export const useConversationTraceStore = create<ConversationTraceState & ConversationTraceActions>((set) => ({
  traces: [],
  latestTrace: null,
  latestTraceByTaskId: {},
  streamTraceByTaskId: {},
  latestTraceByTaskIdAndOperation: {},

  recordTrace: (record) => {
    const nextRecord: ConversationTraceRecord = {
      ...record,
      recordedAt: new Date().toISOString(),
    };
    set((state) => {
      const latestTraceByTaskId = nextRecord.taskId
        ? { ...state.latestTraceByTaskId, [nextRecord.taskId]: nextRecord }
        : state.latestTraceByTaskId;
      const streamTraceByTaskId =
        nextRecord.taskId && nextRecord.operation === "task_stream"
          ? { ...state.streamTraceByTaskId, [nextRecord.taskId]: nextRecord }
          : state.streamTraceByTaskId;
      const latestTraceByTaskIdAndOperation = nextRecord.taskId
        ? {
            ...state.latestTraceByTaskIdAndOperation,
            [nextRecord.taskId]: {
              ...(state.latestTraceByTaskIdAndOperation[nextRecord.taskId] ?? {}),
              [nextRecord.operation]: nextRecord,
            },
          }
        : state.latestTraceByTaskIdAndOperation;

      return {
        traces: [...state.traces, nextRecord],
        latestTrace: nextRecord,
        latestTraceByTaskId,
        streamTraceByTaskId,
        latestTraceByTaskIdAndOperation,
      };
    });
  },

  resetConversationTraces: () => {
    set({
      traces: [],
      latestTrace: null,
      latestTraceByTaskId: {},
      streamTraceByTaskId: {},
      latestTraceByTaskIdAndOperation: {},
    });
  },
}));

/**
 * 获取指定任务最近一次对话请求 trace。
 *
 * @param taskId - 任务标识，空值返回 null。
 * @returns 匹配任务的最近 trace；不存在时返回 null。
 */
export function getConversationTraceForTask(taskId: string | null): ConversationTraceRecord | null {
  if (!taskId) {
    return null;
  }
  return useConversationTraceStore.getState().latestTraceByTaskId[taskId] ?? null;
}

/**
 * 获取指定任务的 SSE stream trace。
 *
 * @param taskId - 任务标识，空值返回 null。
 * @returns 匹配任务的 stream trace；不存在时返回 null。
 */
export function getStreamTraceForTask(taskId: string | null): ConversationTraceRecord | null {
  if (!taskId) {
    return null;
  }
  return useConversationTraceStore.getState().streamTraceByTaskId[taskId] ?? null;
}
