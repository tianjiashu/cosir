/**
 * Workspace 状态管理（Zustand）。
 *
 * 管理每个 workspace 的通用状态（当前承载 CodeGraph 索引准备进度，后续可扩展其他
 * workspace 状态事件），并提供「连 SSE + 触发 prepare」的编排入口：``startEvent`` 先建立
 * `/events/stream` 订阅，再触发 `/events/prepare`，按收到的 preparing/ready/degraded
 * 事件更新状态。
 *
 * 设计约束（对齐后端 workspace 状态事件通道的两段式）：
 * - 必须先连 SSE 再触发 prepare，否则会错过 preparing 事件（bus 无缓冲/重放）。
 * - 同一 workspace 只允许一个活跃事件任务（inflight 集合去重）。
 * - 生命周期：``activeWorkspaceIds`` 标记 workspace 是否仍应存活；``removeEvent``
 *   置为非活跃并断开连接，在途的异步 connect/prepare 完成后据标记中止，避免对
 *   已删除 workspace 写入幽灵状态。
 *
 * @module stores/workspaceEventStore
 */

import { create } from "zustand";
import type {
  WorkspaceEvent,
  WorkspaceState,
  WorkspaceStatus,
} from "@shared/workspaceEvent";
import type { WorkspacePrepareResponse } from "@shared/api";
import { connectWorkspaceEventStream, prepareWorkspace } from "@/services/api";
import { logError, logWarn } from "@/lib/logger";

/** 单个 workspace 的活跃事件任务（SSE 连接清理函数）。 */
interface EventTask {
  /** 断开 SSE 连接的清理函数；connect resolve 后才可用。 */
  cleanup: (() => void) | null;
}

/** workspace 事件 store 状态接口。 */
interface WorkspaceEventStoreState {
  /** 按 workspace_id 维护的状态快照。 */
  statusByWorkspaceId: Record<string, WorkspaceStatus>;
  /** 正在事件编排中的 workspace 集合（幂等去重）。 */
  inflightWorkspaceIds: Set<string>;
  /** 标记 workspace 是否仍应存活（false = 已被删除，在途任务须中止）。 */
  activeWorkspaceIds: Set<string>;
  /** 按 workspace_id 维护的活跃 SSE 连接清理任务。 */
  cleanupByWorkspaceId: Record<string, EventTask>;
}

/** workspace 事件 store 动作接口。 */
interface WorkspaceEventActions {
  /**
   * 触发并跟踪一个 workspace 的准备事件。
   *
   * 幂等：同一 workspace 已有活跃任务或已处于终态时直接返回。
   *
   * @param workspaceId - 需要准备的 workspace 标识。
   * @returns 无。
   *
   * @sideeffect 建立一条到后端的 SSE 订阅并触发一次 /events/prepare；
   *   按事件更新状态；失败时置 error。
   */
  startEvent: (workspaceId: string) => void;
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
    state: WorkspaceState,
    extra?: Partial<WorkspaceStatus>,
  ) => void;
  /**
   * 清理指定 workspace 的事件任务与状态（删除 workspace 时调用）。
   *
   * 同时把 workspace 标记为非活跃，使在途的 connect/prepare 完成后自中止。
   *
   * @param workspaceId - workspace 标识。
   * @returns 无。
   *
   * @sideeffect 断开活跃 SSE 连接、中止在途任务并移除状态快照。
   */
  removeEvent: (workspaceId: string) => void;
  /**
   * 重置整个事件状态。
   *
   * @returns 无。
   *
   * @sideeffect 断开所有活跃 SSE 连接并清空状态。
   */
  resetEvent: () => void;
}

/**
 * workspace 事件 Zustand Store 实例。
 *
 * @returns Zustand hook；组件调用后可读取各 workspace 状态。
 */
export const useWorkspaceEventStore = create<WorkspaceEventStoreState & WorkspaceEventActions>(
  (set, get) => ({
    statusByWorkspaceId: {},
    inflightWorkspaceIds: new Set<string>(),
    activeWorkspaceIds: new Set<string>(),
    cleanupByWorkspaceId: {},

    startEvent: (workspaceId) => {
      if (!workspaceId) {
        return;
      }
      const state = get();
      // 幂等：进行中、或已处于终态（ready/degraded）的 workspace 不再重复触发。
      if (
        state.inflightWorkspaceIds.has(workspaceId) ||
        isTerminalState(state.statusByWorkspaceId[workspaceId]?.state)
      ) {
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

      const onEvent = (event: WorkspaceEvent) => {
        if (!isStillActive()) {
          return;
        }
        set((prev) => ({
          statusByWorkspaceId: {
            ...prev.statusByWorkspaceId,
            [workspaceId]: eventToStatus(event),
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
          setStatusRef(workspaceId, "error", { degradedReason: "workspace 状态事件流连接失败" });
        }
        cleanup?.();
        clearInflight(workspaceId);
      };

      // 用 prepare 的 HTTP 结果自愈置终态；仅当 SSE 尚未推进到终态（状态仍 preparing）时
      // 才写，避免覆盖 SSE 事件已推进的终态。置终态后同步清理连接与 inflight。
      const applyPrepareResult = (resp: WorkspacePrepareResponse | null): void => {
        if (!resp || !isStillActive()) {
          return;
        }
        if (get().statusByWorkspaceId[workspaceId]?.state !== "preparing") {
          return;
        }
        if (!resp.ready) {
          setStatusRef(workspaceId, "degraded", {
            degradedReason: resp.degraded_reason ?? "workspace event kernel unavailable",
          });
        } else {
          setStatusRef(workspaceId, "ready", {
            actionTaken: (resp.action_taken as "init" | "sync" | "none") || "none",
            filesChanged: resp.files_changed,
            durationMs: resp.duration_ms,
          });
        }
        cleanup?.();
        clearInflight(workspaceId);
      };

      // 触发 prepare（独立发起，不依赖 SSE 连接 promise resolve）。
      // Tauri WebView 中 workspace 事件流连接后若无数据推送，``await fetch`` 可能一直
      // 不 resolve；若把 prepare 串行挂在 SSE promise 之后，prepare 会迟迟不触发。
      // 因此 SSE 订阅与 prepare **并行**执行，二者互不阻塞。
      prepareWorkspace(workspaceId)
        .then((resp) => applyPrepareResult(resp))
        .catch((err: unknown) => {
          logError("触发 workspace 事件准备失败", err, {
            module: "workspaceEventStore",
            workspace_id: workspaceId,
          });
          onError();
        });

      // 并行建立 SSE 订阅（收事件流）。只负责收事件与登记断开函数，不承担触发 prepare。
      connectWorkspaceEventStream(workspaceId, onEvent)
        .then((disconnect) => {
          // 连接就绪后若 workspace 已被删除，直接中止。
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
          // prepare 可能已在 SSE 连接 resolve 前完成并置终态（本地索引同步很快），
          // 此处补断开，避免长连接泄漏。
          if (isTerminalState(get().statusByWorkspaceId[workspaceId]?.state)) {
            disconnect();
            clearInflight(workspaceId);
          }
        })
        .catch((err: unknown) => {
          logError("workspace 状态事件流连接失败", err, {
            module: "workspaceEventStore",
            workspace_id: workspaceId,
          });
          onError();
        });
    },

    setStatus: (workspaceId, state, extra) => {
      setStatusRef(workspaceId, state, extra);
    },

    removeEvent: (workspaceId) => {
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

    resetEvent: () => {
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
  state: WorkspaceState,
  extra?: Partial<WorkspaceStatus>,
): void {
  useWorkspaceEventStore.setState((prev) => {
    const status: WorkspaceStatus = {
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
  useWorkspaceEventStore.setState((state) => {
    if (!state.inflightWorkspaceIds.has(workspaceId)) {
      return state;
    }
    const inflight = new Set(state.inflightWorkspaceIds);
    inflight.delete(workspaceId);
    return { inflightWorkspaceIds: inflight };
  });
}

/** 判断状态是否为终态（ready/degraded），终态下不再重复触发事件。 */
function isTerminalState(state: WorkspaceState | undefined): boolean {
  return state === "ready" || state === "degraded";
}

/** 把后端 workspace 状态事件映射为前端归一化状态快照。 */
function eventToStatus(event: WorkspaceEvent): WorkspaceStatus {
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
    default: {
      const _exhaustive: never = event.event_type;
      logWarn("未知 workspace 状态事件", {
        module: "workspaceEventStore",
        event_type: String(_exhaustive),
      });
      return { state: "idle", updatedAt };
    }
  }
}
