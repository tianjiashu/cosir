/**
 * 客户端启动恢复任务 Hook。
 *
 * 唯一职责：在 workspace 列表可用后，恢复客户端首屏 task。
 *
 * 设计背景：上一版把「恢复上次活跃 task」绑定在 App.tsx 的 activeWorkspaceId 任务列表
 * 懒加载流程里，导致「持久化 task 属于非当前 workspace」时首屏只做了列表首项回退，
 * 未真正走 openTask 的完整回填路径。本 hook 把该能力从 App 装配中解耦出来，直接以
 * ``openTask(taskId)`` 为事实源恢复 task，无论它属于哪个 workspace。
 *
 * 触发与去重：以 ``workspaces`` 列表从「空 → 非空」作为唯一触发信号（而非
 * activeWorkspaceId 变化，避免对齐 activeWorkspaceId 时二次触发自身）；hook 内部用
 * ``useRef(false)`` 作为唯一恢复去重点，确保启动生命周期只恢复一次。
 *
 * @module hooks/useStartupTaskResume
 */

import { useEffect, useRef } from "react";
import { useTask } from "@/hooks/useTask";
import { useTaskStore, loadPersistedActiveTaskId } from "@/stores/taskStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { loadWorkspaceTasks } from "@/hooks/useWorkspaceTaskLazyLoad";
import { ServiceError } from "@/services/types";
import { logError, logInfo, logWarn } from "@/lib/logger";

/** 本 hook 的统一日志上下文模块标识。 */
const MODULE = "useStartupTaskResume";

/**
 * 回退打开默认 workspace 的首条 task。
 *
 * 加载目标 workspace 的 task list（经模块级 inflightLoads 去重），成功后取其首条 task
 * 经 ``openTask`` 打开（选中 + 拉取 task/turns + 回填 events）。加载失败、无任务、或
 * ``openTask`` 打开首条 task 失败时，均静默降级到空态（不抛错、不影响主流程）。
 *
 * @param workspaceId - 目标 workspace 标识；为空（无 workspace）时直接返回。
 * @param openTask - 打开任务的函数（由 useTask 提供，经 ref 持有最新引用）。
 *
 * @returns 无返回值；以副作用方式驱动 taskStore / turnStore / eventStore。
 *
 * @throws 不抛出异常：``openTask`` 的失败在内部捕获并记录 error 后降级到空态。
 *
 * @sideeffect
 * - loadWorkspaceTasks(workspaceId)：拉取并缓存该 workspace 的任务列表。
 * - openTask(首条 task)：打开列表首条 task 并回填历史事件。
 */
async function resumeDefaultTask(
  workspaceId: string | undefined,
  openTask: (taskId: string) => Promise<void>,
): Promise<void> {
  if (!workspaceId) {
    return;
  }
  const loaded = await loadWorkspaceTasks(workspaceId);
  if (!loaded) {
    return;
  }
  const tasks = useTaskStore.getState().tasksByWorkspaceId[workspaceId] ?? [];
  const firstTaskId = tasks[0]?.task_id;
  if (firstTaskId) {
    logInfo("回退默认工作区首条任务", { module: MODULE, task_id: firstTaskId, from_persisted: false });
    try {
      await openTask(firstTaskId);
    } catch (err) {
      logError("回退默认工作区首条任务打开失败，降级到空态", err, {
        module: MODULE,
        task_id: firstTaskId,
        from_persisted: false,
      });
    }
  }
}

/**
 * 在启动会话中恢复首屏 task 的编排 hook。
 *
 * 业务流（完整）：
 * 1. 等待 ``workspaces`` 非空（唯一触发信号），且尚未恢复过（ref 去重）。
 * 2. 读取持久化 task id（``loadPersistedActiveTaskId``）：
 *    - 存在：``await openTask(taskId)`` 完成「选中 + 拉取 task/turns + 回填 events」；
 *      成功后从 ``tasksById[taskId].workspace_id`` 对齐 ``activeWorkspaceId``，并后台
 *      ``loadWorkspaceTasks(workspace_id)`` 让 Sidebar 分组对齐中央会话。
 *      - 抛 404（``ServiceError.statusCode === 404``）：清理持久化（``setActiveTask(null)``）
 *        并回退默认 workspace 首条 task。
 *      - 抛其它错误（网络/服务）：不清理持久化（保留下次启动重试机会），降级到默认
 *        task 或空态。
 *    - 不存在：加载默认 workspace（workspaces[0]）的 task list，打开其首条 task。
 * 3. workspace 列表为空：不恢复。
 *
 * @returns 无返回值；以副作用方式驱动 taskStore / workspaceStore / Sidebar 对齐。
 *
 * @throws 不主动抛出：openTask / loadWorkspaceTasks 的异常均在内部捕获并按降级策略处理。
 *
 * @sideeffect
 * - 调 openTask：拉取 task/turns/events 并写入 taskStore/turnStore/eventStore，切换 activeTaskId。
 * - 对齐 workspaceStore.activeWorkspaceId 到持久化 task 所属 workspace。
 * - 后台 loadWorkspaceTasks 让 Sidebar 分组与中央会话对齐。
 * - 恢复失败（404）时经 setActiveTask(null) 清空持久化 active task id。
 */
export function useStartupTaskResume(): void {
  const workspaces = useWorkspaceStore((s) => s.workspaces);
  const setActiveWorkspace = useWorkspaceStore((s) => s.setActiveWorkspace);
  const setActiveTask = useTaskStore((s) => s.setActiveTask);
  const { openTask } = useTask();

  // 唯一恢复去重点：启动生命周期只恢复一次。跨渲染保持，确保重复触发（如重渲染、
  // workspaces 引用变化）不会导致二次恢复。
  const resumedRef = useRef(false);

  // openTask 经 ref 持有最新引用，避免把它放进 effect 依赖数组导致 effect 因
  // openTask 引用变化而误重跑（参考原 App.tsx openTaskRef 模式）。
  const openTaskRef = useRef(openTask);
  openTaskRef.current = openTask;

  useEffect(() => {
    if (resumedRef.current || workspaces.length === 0) {
      return;
    }
    resumedRef.current = true;
    const defaultWorkspaceId = workspaces[0]?.workspace_id;

    void (async () => {
      const persistedId = loadPersistedActiveTaskId();
      if (persistedId) {
        try {
          await openTaskRef.current(persistedId);
          const task = useTaskStore.getState().tasksById[persistedId];
          if (task?.workspace_id) {
            setActiveWorkspace(task.workspace_id);
            logInfo("首屏自动恢复活跃任务并回放历史", {
              module: MODULE,
              task_id: persistedId,
              from_persisted: true,
            });
            void loadWorkspaceTasks(task.workspace_id);
          }
        } catch (err) {
          if (err instanceof ServiceError && err.statusCode === 404) {
            logWarn("恢复的持久化任务已不存在，清理持久化并回退默认任务", {
              module: MODULE,
              task_id: persistedId,
            });
            setActiveTask(null);
            await resumeDefaultTask(defaultWorkspaceId, openTaskRef.current);
          } else {
            logError("恢复持久化任务失败，保留持久化值并降级", err, {
              module: MODULE,
              task_id: persistedId,
            });
            await resumeDefaultTask(defaultWorkspaceId, openTaskRef.current);
          }
        }
      } else {
        await resumeDefaultTask(defaultWorkspaceId, openTaskRef.current);
      }
    })();
    // 触发条件严格限定为 workspaces 列表从「空 → 非空」；openTask/setActiveWorkspace 等
    // 经 ref 或 zustand 稳定引用持有，不进入依赖，避免恢复过程中引用变化导致二次触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaces]);
}
