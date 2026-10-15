"use client";

import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";

import type { WorkspaceCatalog } from "@/components/layout/use-workspace-catalog";
import { deleteTask, deleteWorkspace, forkTask, type StartedConversation } from "@/lib/api/workspaces";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { HttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
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
  const closeWorkspaceTabs = useWorkbenchStore((state) => state.closeWorkspace);
  const [forkingRunId, setForkingRunId] = useState<number | null>(null);
  const { load, selectWorkspace, selectedWorkspaceId, workspaces } = catalog;

  const startConversation = useCallback((conversation: StartedConversation, disabledToolGroups: string[]) => {
    navigate(`/tasks/${conversation.task_id}`, { state: { initialDisabledToolGroups: disabledToolGroups } });
    void load();
  }, [load, navigate]);

  const forkRun = useCallback(async (runId: number) => {
    if (focusedTaskId === null || forkingRunId !== null) return;
    setForkingRunId(runId);
    try {
      const target = await forkTask(focusedTaskId, runId);
      await load();
      navigate(`/tasks/${target.task_id}`);
    } catch (cause) {
      void frontendLog("ERROR", "task_fork_failed", "Fork 任务失败", {
        data: { taskId: focusedTaskId, runId, status: cause instanceof HttpError ? cause.status : null },
        error: cause,
      });
      if (cause instanceof HttpError && cause.status === 409) {
        // 409 是状态冲突：快照未就绪或仍有运行中的 Run，刷新侧栏让 fork 可用性回到后端事实。
        notify.error("Fork 失败", {
          error: new UserFacingError(cause.code === "SNAPSHOT_NOT_READY"
            ? "对话快照仍在准备中，请稍后重试。"
            : "当前任务仍有运行中的轮次，暂时不能 Fork。"),
        });
        await load();
      } else {
        notify.error("Fork 失败", {
          error: cause,
          fallback: "Fork 失败，请重试",
          action: { label: "重试", onClick: () => void forkRun(runId) },
        });
      }
    } finally {
      setForkingRunId(null);
    }
  }, [focusedTaskId, forkingRunId, load, navigate]);

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
    if (target.kind === "workspace") {
      closeWorkspaceTabs(target.id);
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

  return { startConversation, forkRun, forkingRunId, requestDelete };
}
