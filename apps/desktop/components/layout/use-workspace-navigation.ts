import { useCallback, useEffect, useRef } from "react";
import { matchPath, useBlocker } from "react-router-dom";
import { getTask } from "@/lib/api/workspaces";
import { confirmDiscardUnsaved, hasUnsavedChanges } from "@/lib/unsaved-changes";
import { confirmWorkbenchWorkspaceChange, hasWorkbenchDrafts } from "@/lib/workbench/drafts";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import type { WorkspaceCatalog } from "./use-workspace-catalog";

const GUARDED_SCOPES = ["settings", "dialog"] as const;

/**
 * 路由离开和无路由的工作区选择共用丢弃边界；确认前不改变目录选择或销毁工作台会话。
 * 路由 blocker 同时覆盖侧栏、命令面板、浏览器前进/后退；未知任务先解析所属工作区。
 * 配置/观测路由保活最近任务，因此有保活任务时不改变工作台工作区。
 */
export function useWorkspaceNavigation(catalog: WorkspaceCatalog, focusedTaskId: number | null) {
  const blocker = useBlocker(({ currentLocation, nextLocation }) => (
    currentLocation.pathname !== nextLocation.pathname
    && (GUARDED_SCOPES.some(hasUnsavedChanges) || hasWorkbenchDrafts())
  ));
  const handled = useRef(false);
  const selecting = useRef(false);
  useEffect(() => {
    if (blocker.state !== "blocked") {
      handled.current = false;
      return;
    }
    if (handled.current) return;
    handled.current = true;
    void (async () => {
      try {
        for (const scope of GUARDED_SCOPES) {
          if (!(await confirmDiscardUnsaved(scope))) {
            blocker.reset();
            return;
          }
        }
        if (hasWorkbenchDrafts()) {
          const path = blocker.location.pathname;
          const taskMatch = matchPath("/tasks/:taskId", path);
          const workspaceMatch = matchPath("/workspaces/:workspaceId/settings/*", path);
          let workspaceId = catalog.activeTaskWorkspaceId;
          if (taskMatch) {
            const taskId = Number(taskMatch.params.taskId);
            const known = catalog.workspaces.flatMap(workspace => workspace.tasks).find(task => task.task_id === taskId);
            workspaceId = (known ?? await getTask(taskId)).workspace_id;
          } else if (path === "/") {
            workspaceId = catalog.selectedWorkspaceId;
          } else if (focusedTaskId === null && workspaceMatch) {
            workspaceId = Number(workspaceMatch.params.workspaceId);
          }
          if (!(await confirmWorkbenchWorkspaceChange(workspaceId))) {
            blocker.reset();
            return;
          }
        }
        blocker.proceed();
      } catch (error) {
        void frontendLog("ERROR", "workspace_navigation_failed", "切换前无法确定目标工作区，保留当前草稿", { error });
        notify.error("无法切换工作区", { error, fallback: "无法读取目标任务，请重试" });
        blocker.reset();
      }
    })();
  }, [blocker, catalog, focusedTaskId]);

  const selectWorkspace = useCallback((workspaceId: number | null) => {
    if (selecting.current) return;
    selecting.current = true;
    void (async () => {
      try {
        // 有任务保活时，目录选择只影响后续新对话，不切换当前工作台。
        if (focusedTaskId === null && !(await confirmWorkbenchWorkspaceChange(workspaceId))) return;
        catalog.selectWorkspace(workspaceId);
      } finally {
        selecting.current = false;
      }
    })();
  }, [catalog, focusedTaskId]);
  return selectWorkspace;
}
