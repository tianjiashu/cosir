/**
 * Workspace 索引状态管理（Zustand）。
 *
 * 管理每个 workspace 的 CodeGraph 索引进度状态，并提供「连 SSE + 触发 prepare」
 * 的编排入口：``startIndexing`` 先建立 `/index/stream` 订阅，再触发
 * `/index/prepare`，按收到的 preparing/ready/degraded 事件更新状态。
 *
 * 设计约束（对齐后端 workspace_index_api 的两段式）：
 * - 必须先连 SSE 再触发 prepare，否则会错过 preparing 事件（bus 无缓冲/重放）。
 * - 同一 workspace 只允许一个活跃索引任务（inflight 集合去重）。
 * - 生命周期：``activeWorkspaceIds`` 标记 workspace 是否仍应存活；``removeIndex``
 *   置为非活跃并断开连接，在途的异步 connect/prepare 完成后据标记中止，避免对
 *   已删除 workspace 写入幽灵状态。
 *
 * @module stores/workspaceIndexStore
 */

import { create } from "zustand";
import type {
  WorkspaceIndexEvent,
  WorkspaceIndexState,
  WorkspaceIndexStatus,
} from "@shared/codegraph";
import { connectWorkspaceIndexStream, prepareWorkspaceIndex } from "@/services/api";
import { logError, logWarn } from "@/lib/logger";

/** 单个 workspace 的活跃索引任务（SSE 连接清理函数）。 */
interface IndexTask {
  /** 断开 SSE 连接的清理函数；connect resolve 后才可用。 */
  cleanup: (() => void) | null;
}

/** workspace 索引 store 状态接口。 */
interface WorkspaceIndexStoreState {
  /** 按 workspace_id 维护的索引状态快照。 */
  statusByWorkspaceId: Record<string, WorkspaceIndexStatus>;
  /** 正在索引编排中的 workspace 集合（幂等去重）。 */
  inflightWorkspaceIds: Set<string>;
  /** 标记 workspace 是否仍应存活（false = 已被删除，在途任务须中止）。 */
  activeWorkspaceIds: Set<string>;
  /** 按 workspace_id 维护的活跃 SSE 连接清理任务。 */
  cleanupByWorkspaceId: Record<string, IndexTask>;
}

/** workspace 索引 store 动作接口。 */
interface WorkspaceIndexActions {
  /**
   * 触发并跟踪一个 workspace 的索引进度。
   *
   * 幂等：同一 workspace 已有活跃任务或已处于终态时直接返回。
   *
   * @param workspaceId - 需要索引的 workspace 标识。
   * @returns 无。
   *
   * @sideeffect 建立一条到后端的 SSE 订阅并触发一次 /index/prepare；
   *   按事件更新状态；失败时置 error。
   */
  startIndexing: (workspaceId: string) => void;
  /**
   * 直接置为指定状态（用于错误兜底等外部事件）。
   *
   * @param workspaceId - workspace 标识。
   * @param state - 目标归一化状态。
   * @param extra - 可选补充字段（reason / filesChanged 等）。
   * @returns 无。
   *
   * @sideeffect 更新内存状态快照。
   */
  setStatus: (
    workspaceId: string,
    state: WorkspaceIndexState,
    extra?: Partial<WorkspaceIndexStatus>,
  ) => void;
  /**
   * 清理指定 workspace 的索引任务与状态（删除 workspace 时调用）。
   *
   * 同时把 workspace 标记为非活跃，使在途的 connect/prepare 完成后自中止。
   *
   * @param workspaceId - workspace 标识。
   * @returns 无。
   *
   * @sideeffect 断开活跃 SSE 连接、中止在途任务并移除状态快照。
   */
  removeIndex: (workspaceId: string) => void;
  /**
   * 重置整个索引状态。
   *
   * @returns 无。
   *
   * @sideeffect 断开所有活跃 SSE 连接并清空状态。
   */
  resetIndex: () => void;
}

/**
 * workspace 索引 Zustand Store 实例。
 *
 * @returns Zustand hook；组件调用后可读取各 workspace 索引状态。
 */
export const useWorkspaceIndexStore = create<WorkspaceIndexStoreState & WorkspaceIndexActions>(
  (set, get) => ({
    statusByWorkspaceId: {},
    inflightWorkspaceIds: new Set<string>(),
    activeWorkspaceIds: new Set<string>(),
    cleanupByWorkspaceId: {},

    startIndexing: (workspaceId) => {
      if (!workspaceId) {
        return;
      }
      const state = get();
      // 幂等：进行中、或已处于终态（ready/degraded）的 workspace 不再重复触发。
      if (state.inflightWorkspaceIds.has(workspaceId) || isTerminalState(state.statusByWorkspaceId[workspaceId]?.state)) {
        return;
      }
      // 标记进行中 + 活跃，避免并发重复起连接、并供在途任务判断存活。
      set((prev) => ({
        inflightWorkspaceIds: new Set(prev.inflightWorkspaceIds).add(workspaceId),
        activeWorkspaceIds: new Set(prev.activeWorkspaceIds).add(workspaceId),
        statusByWorkspaceId: {
          ...prev.statusByWorkspaceId,
          [workspaceId]: { state: "preparing", updatedAt: new Date().toISOString() },
        },
      }));

      let cleanup: (() => void) | null = null;

      const isStillActive = () => get().activeWorkspaceIds.has(workspaceId);

      const onEvent = (event: WorkspaceIndexEvent) => {
        if (!isStillActive()) {
          return;
        }
        set((prev) => ({
          statusByWorkspaceId: {
            ...prev.statusByWorkspaceId,
            [workspaceId]: indexEventToStatus(event),
          },
        }));
        if (event.event_type === "workspace_ready" || event.event_type === "workspace_degraded") {
          // 终态：断开连接并释放 inflight 标记。
          cleanup?.();
          clearInflight(workspaceId);
        }
      };

      const onError = () => {
        // 仅当状态仍处于 preparing（SSE 未推送终态）时，才用失败结果置 error，
        // 避免覆盖 SSE 已推进的 ready/degraded 终态。
        if (isStillActive() && get().statusByWorkspaceId[workspaceId]?.state === "preparing") {
          setStatusRef(workspaceId, "error", { degradedReason: "索引进度流连接失败" });
        }
        cleanup?.();
        clearInflight(workspaceId);
      };

      // 先连 SSE（确保订阅就绪），再触发 prepare，避免错过 preparing 事件。
      connectWorkspaceIndexStream(workspaceId, onEvent)
        .then((disconnect) => {
          // 连接就绪后若 workspace 已被删除，直接中止，不写状态也不触发 prepare。
          if (!isStillActive()) {
            disconnect();
            return;
          }
          cleanup = disconnect;
          const prevTask = get().cleanupByWorkspaceId[workspaceId];
          if (prevTask?.cleanup) {
            prevTask.cleanup();
          }
          set((prev) => ({
            cleanupByWorkspaceId: {
              ...prev.cleanupByWorkspaceId,
              [workspaceId]: { cleanup: disconnect },
            },
          }));
          return prepareWorkspaceIndex(workspaceId);
        })
        .then((resp) => {
          // 自愈：仅当 SSE 尚未推进到终态（状态仍 preparing）时，才用 prepare 的
          // HTTP 结果置 ready/degraded，避免覆盖 SSE 事件已推进的终态。
          if (!resp || !isStillActive()) {
            return;
          }
          if (get().statusByWorkspaceId[workspaceId]?.state !== "preparing") {
            return;
          }
          if (!resp.ready) {
            setStatusRef(workspaceId, "degraded", {
              degradedReason: resp.degraded_reason ?? "workspace_event kernel unavailable",
            });
          } else {
            setStatusRef(workspaceId, "ready", {
              actionTaken: (resp.action_taken as "init" | "sync" | "none") || "none",
              filesChanged: resp.files_changed,
              durationMs: resp.duration_ms,
            });
          }
          // 自愈置终态后，与 onEvent/onError 终态分支保持一致地清理连接与 inflight，
          // 否则在「SSE 未推送终态」这一自愈兜底场景下长连接与 state 会泄漏。
          cleanup?.();
          clearInflight(workspaceId);
        })
        .catch((err: unknown) => {
          logError("触发 workspace 索引准备失败", err, {
            module: "workspaceIndexStore",
            workspace_id: workspaceId,
          });
          onError();
        });
    },

    setStatus: (workspaceId, state, extra) => {
      setStatusRef(workspaceId, state, extra);
    },

    removeIndex: (workspaceId) => {
      // 先标记非活跃，使在途 connect/prepare 完成后自中止。
      set((prev) => {
        const active = new Set(prev.activeWorkspaceIds);
        active.delete(workspaceId);
        const inflight = new Set(prev.inflightWorkspaceIds);
        inflight.delete(workspaceId);
        return { activeWorkspaceIds: active, inflightWorkspaceIds: inflight };
      });
      const task = get().cleanupByWorkspaceId[workspaceId];
      if (task?.cleanup) {
        task.cleanup();
      }
      set((prev) => {
        const statusNext = { ...prev.statusByWorkspaceId };
        delete statusNext[workspaceId];
        const cleanupNext = { ...prev.cleanupByWorkspaceId };
        delete cleanupNext[workspaceId];
        return { statusByWorkspaceId: statusNext, cleanupByWorkspaceId: cleanupNext };
      });
    },

    resetIndex: () => {
      const tasks = get().cleanupByWorkspaceId;
      for (const task of Object.values(tasks)) {
        task.cleanup?.();
      }
      set({
        statusByWorkspaceId: {},
        inflightWorkspaceIds: new Set(),
        activeWorkspaceIds: new Set(),
        cleanupByWorkspaceId: {},
      });
    },
  }),
);

/** 直接写指定 workspace 的状态快照（构造显式字段避免类型拓宽）。 */
function setStatusRef(
  workspaceId: string,
  state: WorkspaceIndexState,
  extra?: Partial<WorkspaceIndexStatus>,
): void {
  useWorkspaceIndexStore.setState((prev) => {
    const status: WorkspaceIndexStatus = {
      state,
      updatedAt: new Date().toISOString(),
      ...(extra?.actionTaken !== undefined && { actionTaken: extra.actionTaken }),
      ...(extra?.filesChanged !== undefined && { filesChanged: extra.filesChanged }),
      ...(extra?.durationMs !== undefined && { durationMs: extra.durationMs }),
      ...(extra?.degradedReason !== undefined && { degradedReason: extra.degradedReason }),
    };
    return {
      statusByWorkspaceId: {
        ...prev.statusByWorkspaceId,
        [workspaceId]: status,
      },
    };
  });
}

/** 清除 workspace 的 inflight 标记（幂等）。 */
function clearInflight(workspaceId: string): void {
  useWorkspaceIndexStore.setState((state) => {
    if (!state.inflightWorkspaceIds.has(workspaceId)) {
      return state;
    }
    const inflight = new Set(state.inflightWorkspaceIds);
    inflight.delete(workspaceId);
    return { inflightWorkspaceIds: inflight };
  });
}

/** 判断状态是否为终态（ready/degraded），终态下不再重复触发索引。 */
function isTerminalState(state: WorkspaceIndexState | undefined): boolean {
  return state === "ready" || state === "degraded";
}

/** 把后端索引进度事件映射为前端归一化状态快照。 */
function indexEventToStatus(event: WorkspaceIndexEvent): WorkspaceIndexStatus {
  const updatedAt = new Date().toISOString();
  switch (event.event_type) {
    case "workspace_preparing":
      return { state: "preparing", updatedAt };
    case "workspace_ready": {
      const p = event.payload as {
        action_taken?: "init" | "sync";
        files_changed?: number;
        duration_ms?: number;
      };
      return {
        state: "ready",
        actionTaken: p.action_taken ?? "none",
        filesChanged: p.files_changed,
        durationMs: p.duration_ms,
        updatedAt,
      };
    }
    case "workspace_degraded": {
      const p = event.payload as { degraded_reason?: string; state?: string };
      return { state: "degraded", degradedReason: p.degraded_reason ?? p.state ?? "unknown", updatedAt };
    }
    default:
      logWarn("未知 workspace 索引进度事件", {
        module: "workspaceIndexStore",
        event_type: event.event_type,
      });
      return { state: "idle", updatedAt };
  }
}
