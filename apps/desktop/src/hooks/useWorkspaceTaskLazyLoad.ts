import { useCallback, useEffect, useSyncExternalStore } from "react";
import { useTaskStore } from "@/stores/taskStore";
import * as api from "@/services/api";
import { logError } from "@/lib/logger";
import type { WorkspaceRecord } from "@shared/workspace";

/**
 * 工作区任务列表惰性加载 hook。
 *
 * 职责边界：只负责「按 workspace 分组按需拉取并缓存任务列表」这一副作用编排，
 * 不渲染任何 UI、不处理删除/选中等业务。组件层（Sidebar）调用本 hook 即可，
 * 避免把在途去重、失败日志等业务流程塞进表现层组件。
 *
 * 加载态契约：以 ``taskStore.isWorkspaceLoaded(wsId)`` 为准（由 store 的
 * ``loadedWorkspaceIds`` 维护），与数据态正交；本 hook 与 App 首屏恢复共用同一
 * 去重入口（``inflightLoads`` 模块级），确保同一 workspace 不会并发重复请求。
 * 失败态由模块级 ``failedWorkspaceIds`` 记录，经 ``useSyncExternalStore`` 暴露给
 * 组件，使加载失败可显式呈现并支持重试（避免失败被永久隐藏为「加载中」）。
 *
 * @module hooks/useWorkspaceTaskLazyLoad
 */

/**
 * 模块级在途请求去重集合：跨组件实例（App 与 Sidebar）共享，避免双入口并发重复请求。
 * 键为 workspace_id（后端 int 主键，§3.7 收敛为 number）。
 */
const inflightLoads = new Set<number>();

/**
 * 模块级加载失败集合：记录最近一次请求失败的 workspace，供组件渲染「加载失败·重试」。
 * 成功加载后从集合移除；属可变外部状态，须经 useSyncExternalStore 订阅方可触发重渲染。
 * 键为 workspace_id（后端 int 主键，§3.7 收敛为 number）。
 */
const failedWorkspaceIds = new Set<number>();
/**
 * 版本化不可变快照：useSyncExternalStore 要求 getSnapshot 在同一状态下返回同一引用、
 * 状态变化后返回不同引用（否则 React 用 Object.is 比对会误判无变化而 bail out 不重渲染）。
 * 因此每次变更后重建新 Set 并缓存于此，getSnapshot 返回该缓存而非原地 mutate 的源集合。
 */
let failedSnapshot: ReadonlySet<number> = new Set(failedWorkspaceIds);
/** 订阅失败集合的监听器集合（useSyncExternalStore 要求）。 */
const failedListeners = new Set<() => void>();

/**
 * 提交失败集合的变更：重建不可变快照并通知订阅者。
 * 必须先重建快照再 emit，确保 getSnapshot 返回新引用以驱动重渲染。
 */
function commitFailedChange(): void {
  failedSnapshot = new Set(failedWorkspaceIds);
  for (const listener of failedListeners) {
    listener();
  }
}

/** useSyncExternalStore 的 getSnapshot：返回版本化不可变快照。 */
function getFailedSnapshot(): ReadonlySet<number> {
  return failedSnapshot;
}

/** 暴露当前失败 workspace 集合的只读快照（测试/调试用，便于验证加载失败态可见性）。 */
export function getFailedWorkspaceIds(): ReadonlySet<number> {
  return failedSnapshot;
}

/**
 * 拉取并写入指定 workspace 的任务列表（权威 loader，供 hook 与 App 复用）。
 *
 * 在途请求经模块级 ``inflightLoads`` 去重：请求返回前同一 workspace 的重复调用直接跳过，
 * 不并发发起多次 HTTP。App 首屏恢复与 hook 自动补拉共用此入口，故二者不会重复请求同一
 * workspace。加载失败仅记错误日志、记入 failedWorkspaceIds、返回 ``false``，不 reject，
 * 调用方可据此重试（ensureLoaded 触发）/ 降级。
 *
 * @param workspaceId - 目标 workspace 标识（后端 int 主键）。
 * @returns 加载成功返回 true；已加载（跳过）/在途（跳过）/加载失败返回 false。
 */
export async function loadWorkspaceTasks(workspaceId: number): Promise<boolean> {
  if (useTaskStore.getState().isWorkspaceLoaded(workspaceId)) {
    return false;
  }
  if (inflightLoads.has(workspaceId)) {
    return false;
  }
  inflightLoads.add(workspaceId);
  try {
    const tasks = await api.listWorkspaceTasks(workspaceId);
    failedWorkspaceIds.delete(workspaceId);
    commitFailedChange();
    useTaskStore.getState().setWorkspaceTasks(workspaceId, tasks);
    return true;
  } catch (err) {
    logError("惰性加载工作区任务失败", err, { module: "useWorkspaceTaskLazyLoad", workspace_id: workspaceId });
    failedWorkspaceIds.add(workspaceId);
    commitFailedChange();
    return false;
  } finally {
    inflightLoads.delete(workspaceId);
  }
}

/** 注册失败集合的订阅者，返回注销函数（useSyncExternalStore 的 subscribe 契约）。 */
function subscribeFailed(callback: () => void): () => void {
  failedListeners.add(callback);
  return () => failedListeners.delete(callback);
}

/** 清除指定 workspace 的失败标记（如该 workspace 被删除/重建时防止陈旧失败态残留）。 */
export function clearFailedWorkspaceId(workspaceId: number): void {
  if (failedWorkspaceIds.delete(workspaceId)) {
    commitFailedChange();
  }
}

/** 确保指定 workspace 的任务列表已加载（未加载则拉取并写入分组缓存）。 */
type EnsureLoaded = (workspaceId: number) => Promise<void>;

/**
 * 工作区任务惰性加载 hook。
 *
 * @param workspaces - 当前工作区列表（来自 workspaceStore）。
 * @param collapsedWorkspaceIds - 折叠状态集合（来自 workspaceStore，键为后端 int 主键）。
 * @returns 命令式触发 ``ensureLoaded(workspaceId)`` 与当前 ``failedWorkspaceIds`` 失败快照。
 */
export function useWorkspaceTaskLazyLoad(
  workspaces: WorkspaceRecord[],
  collapsedWorkspaceIds: Set<number>,
): { ensureLoaded: EnsureLoaded; failedWorkspaceIds: ReadonlySet<number> } {
  const ensureLoaded = useCallback((workspaceId: number) => loadWorkspaceTasks(workspaceId).then(() => undefined), []);

  // 挂载/列表变化时，对「处于展开态且尚未加载」的 workspace 触发惰性加载。
  // 默认展开（collapsedWorkspaceIds 初始为空）的工作区在此补齐首屏加载；
  // ensureLoaded 内部以 isWorkspaceLoaded 单点守门，effect 不再重复判定加载态。
  useEffect(() => {
    for (const workspace of workspaces) {
      const isCollapsed = collapsedWorkspaceIds.has(workspace.workspace_id);
      if (!isCollapsed) {
        void ensureLoaded(workspace.workspace_id);
      }
    }
  }, [workspaces, collapsedWorkspaceIds, ensureLoaded]);

  // 订阅模块级失败集合，使「加载失败」可驱动重渲染（避免失败永久隐藏为加载中）。
  const failedWorkspaceIds = useSyncExternalStore(subscribeFailed, getFailedSnapshot);

  return { ensureLoaded, failedWorkspaceIds };
}
