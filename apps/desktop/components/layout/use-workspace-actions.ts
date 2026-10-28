"use client";

import { useFilesStore } from "@/lib/workspace-files/store";

import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import type { WorkspaceCatalog } from "@/components/layout/use-workspace-catalog";
import { deleteTask, deleteWorkspace, forkTask, type StartedConversation } from "@/lib/api/workspaces";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { HttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { useBackgroundTasksStore } from "@/lib/task-overview/background-tasks-store";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { useWorkbenchStore } from "@/lib/workbench/store";

export type DeleteTarget =
  | { kind: "workspace"; id: number; label: string; taskCount: number }
  | { kind: "task"; id: number; label: string };

/**
 * 工作区 / 任务的用户动作：开始对话、分叉、删除。
 *
 * 只编排「请求 → 刷新目录 → 导航 → 反馈」，目录状态由 useWorkspaceCatalog 持有；
 * 失败统一经 notify / 确认框呈现，并写结构化日志。
 */
export function useWorkspaceActions(catalog: WorkspaceCatalog, focusedTaskId: number | null) {
  const navigate = useNavigate();
  const location = useLocation();
  const currentLocationKey = useRef<string | null>(location.key);
  const forkPending = useRef(false);
  useLayoutEffect(() => {
    currentLocationKey.current = location.key;
    return () => { currentLocationKey.current = null; };
  }, [location.key]);
  const closeWorkspaceTabs = useWorkbenchStore((state) => state.closeWorkspace);
  const [forkingMessageId, setForkingMessageId] = useState<string | null>(null);
  const { load, selectWorkspace, selectedWorkspaceId, workspaces } = catalog;

  const startConversation = useCallback((conversation: StartedConversation) => {
    navigate(`/tasks/${conversation.task_id}`);
    void load();
  }, [load, navigate]);

  // 请求绑定发起时的 Task 和路由；用户切页后迟到的响应只更新目录，不抢回页面。
  const executeFork = useCallback(async function executeFork(
    sourceTaskId: number, messageId: string, originLocationKey: string,
  ) {
    if (forkPending.current) return;
    forkPending.current = true;
    setForkingMessageId(messageId);
    try {
      const target = await forkTask(sourceTaskId, messageId);
      // 创建已成功，目录刷新由 catalog 自行处理错误，不应影响成功反馈或触发重复创建。
      void load();
      if (currentLocationKey.current === originLocationKey) {
        navigate(`/tasks/${target.task_id}`);
      } else {
        notify.success("Fork 任务已创建");
      }
    } catch (cause) {
      void frontendLog("ERROR", "task_fork_failed", "Fork 任务失败", {
        data: { taskId: sourceTaskId, messageId, status: cause instanceof HttpError ? cause.status : null },
        error: cause,
      });
      if (cause instanceof HttpError && cause.status === 409) {
        const message = cause.code === "WORKSPACE_BUSY"
          ? "工作区正在处理其他操作，请稍后重试。"
          : "所选历史范围内仍有未结束的轮次，暂时不能 Fork。";
        notify.error("Fork 失败", { error: new UserFacingError(message) });
      } else {
        notify.error("Fork 失败", {
          error: cause,
          fallback: "Fork 失败，请重试",
          action: {
            label: "重试",
            onClick: () => void executeFork(sourceTaskId, messageId, originLocationKey),
          },
        });
      }
    } finally {
      forkPending.current = false;
      setForkingMessageId(null);
    }
  }, [load, navigate]);

  const forkMessage = useCallback(async (messageId: string) => {
    if (focusedTaskId === null) return;
    await executeFork(focusedTaskId, messageId, location.key);
  }, [executeFork, focusedTaskId, location.key]);

  /**
   * 删除工作区或任务：确认框托管 busy 与失败提示（失败时保持打开可重试）。
   * 删除成功后才关闭工作台标签、离开已删除的当前页并刷新侧栏。
   */
  const requestDelete = useCallback(async (target: DeleteTarget) => {
    const deletingCurrentWorkspace = target.kind === "workspace" && (
      selectedWorkspaceId === target.id
      || workspaces.some((workspace) => workspace.workspace_id === target.id && workspace.tasks.some((task) => task.task_id === focusedTaskId))
    );
    const confirmed = await requestConfirmation({
      title: target.kind === "workspace" ? `删除工作区“${target.label}”？` : `删除任务“${target.label}”？`,
      description: target.kind === "workspace"
        ? `此操作将永久删除该工作区及其下的 ${target.taskCount} 个任务和全部对话数据。`
        : "此操作将永久删除该任务及其全部对话数据，不影响所属工作区和其他任务。",
      warning: "删除后无法撤销。",
      confirmLabel: "确认删除",
      busyLabel: "删除中…",
      tone: "destructive",
      icon: "delete",
      failureFallback: "删除失败，请重试",
      action: async () => {
        try {
          if (target.kind === "workspace") await deleteWorkspace(target.id);
          else await deleteTask(target.id);
        } catch (cause) {
          void frontendLog("ERROR", "workspace_resource_delete_failed", "删除工作区资源失败", {
            data: { kind: target.kind, id: target.id },
            error: cause,
          });
          throw cause;
        }
      },
    });
    if (!confirmed) return;
    // 终端工作台与后台工作看板按 taskId 分桶的 UI 状态随 Task 删除一并丢弃（后端已清理对应事实）。
    const deletedTaskIds = target.kind === "task"
      ? [target.id]
      : workspaces.find((workspace) => workspace.workspace_id === target.id)?.tasks.map((task) => task.task_id) ?? [];
    useTerminalWorkbenchStore.getState().forget(deletedTaskIds);
    useBackgroundTasksStore.getState().forget(deletedTaskIds);
    if (target.kind === "workspace") {
      closeWorkspaceTabs(target.id);
      useFilesStore.getState().forget(target.id);
      if (deletingCurrentWorkspace) {
        selectWorkspace(null);
        navigate("/");
      }
    } else if (focusedTaskId === target.id) {
      navigate("/");
    }
    notify.success(target.kind === "workspace" ? "工作区已删除" : "任务已删除");
    await load();
  }, [closeWorkspaceTabs, focusedTaskId, load, navigate, selectWorkspace, selectedWorkspaceId, workspaces]);

  return { startConversation, forkMessage, forkingMessageId, requestDelete };
}
