import { forgetSessionComposerDrafts } from "@/lib/assistant/composer/composer-drafts";
import { forgetCommands } from "@/lib/assistant/command-request-store";
import { useDisclosureStore } from "@/lib/assistant/turn/disclosure-store";
import { useFilesStore } from "@/lib/workspace-files/workspace-files-store";

import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import type { WorkspaceCatalog } from "@/components/layout/use-workspace-catalog";
import { createWorkspace, deleteSession, deleteWorkspace, forkSession } from "@/lib/api/workspaces";
import { useModelSelectionStore } from "@/lib/model/model-selection-store";
import { usePinnedSessionsStore } from "@/lib/pinned-sessions-store";
import { confirmDelete } from "@/lib/confirm/confirm-store";
import { desktopHost } from "@/lib/desktop-host";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { HttpError } from "@/lib/http/http-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { forgetMessageHeights } from "@/lib/assistant/turn/message-height-cache";
import { useBackgroundTasksStore } from "@/lib/background-tasks/background-tasks-store";
import { useHandoffStore } from "@/lib/handoff/handoff-store";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { useWorkbenchStore } from "@/lib/workbench/workbench-store";

export type DeleteTarget =
  | { kind: "workspace"; id: number; label: string; sessionCount: number }
  /** `forkCount` 只用于询问是否一起删除；删哪些以后端为准（它重新收集 fork 后代）。 */
  | { kind: "session"; id: number; label: string; forkCount: number };

/**
 * 工作区 / 会话的用户动作：从文件夹添加工作区、分叉、删除。
 *
 * 只编排「请求 → 导航 → 反馈」：目录不在这里重读，写入提交后由后端推送失效（见 useWorkspaceCatalog），
 * 和后台会话 引起的变化走同一条路。失败统一经 notify / 确认框呈现，并写结构化日志。
 */
export function useWorkspaceActions(catalog: WorkspaceCatalog, focusedSessionId: number | null) {
  const navigate = useNavigate();
  const location = useLocation();
  const currentLocationKey = useRef<string | null>(location.key);
  const forkPending = useRef(false);
  useLayoutEffect(() => {
    currentLocationKey.current = location.key;
    return () => { currentLocationKey.current = null; };
  }, [location.key]);
  const [forkingMessageId, setForkingMessageId] = useState<string | null>(null);
  const addPending = useRef(false);
  const [addingWorkspace, setAddingWorkspace] = useState(false);
  const { selectWorkspace, selectedWorkspaceId, workspaces } = catalog;

  /**
   * 从文件夹添加工作区（侧栏区头与新会话页选择器共用）：系统目录选择 → 创建 → 选中新工作区并来到
   * 新会话页（它随推送进入目录）。用户取消目录选择不算失败；返回是否真的添加了一个。
   * 进行中的再次调用直接忽略，两处入口据 `addingWorkspace` 显示忙碌。
   */
  const addWorkspaceFromFolder = useCallback(async (): Promise<boolean> => {
    if (addPending.current) return false;
    addPending.current = true;
    setAddingWorkspace(true);
    try {
      if (!desktopHost) throw new UserFacingError("只能在桌面应用中添加工作区");
      const picked = await desktopHost.dialog.openDirectory({ title: "选择工作区文件夹" });
      if (picked === null) return false;
      const name = picked.split(/[\\/]/).filter(Boolean).at(-1) ?? "新工作区";
      const workspace = await createWorkspace({ name, root_path: picked });
      selectWorkspace(workspace.workspace_id);
      if (location.pathname !== "/") navigate("/");
      void frontendLog("INFO", "workspace_created_from_folder", "已从文件夹添加工作区", {
        data: { workspaceId: workspace.workspace_id },
      });
      return true;
    } catch (cause) {
      void frontendLog("ERROR", "workspace_create_from_folder_failed", "从文件夹创建工作区失败", { error: cause });
      notify.error("添加工作区失败", { error: cause, fallback: "创建工作区失败，请重试" });
      return false;
    } finally {
      addPending.current = false;
      setAddingWorkspace(false);
    }
  }, [location.pathname, navigate, selectWorkspace]);

  // 请求绑定发起时的 Session 和路由；用户切页后迟到的响应只更新目录，不抢回页面。
  const executeFork = useCallback(async function executeFork(
    sourceSessionId: number, messageId: string, originLocationKey: string,
  ) {
    if (forkPending.current) return;
    forkPending.current = true;
    setForkingMessageId(messageId);
    try {
      const target = await forkSession(sourceSessionId, messageId);
      if (currentLocationKey.current === originLocationKey) {
        navigate(`/sessions/${target.session_id}`);
      } else {
        notify.success("Fork 会话已创建");
      }
    } catch (cause) {
      void frontendLog("ERROR", "session_fork_failed", "Fork 会话失败", {
        data: { sessionId: sourceSessionId, messageId, status: cause instanceof HttpError ? cause.status : null },
        error: cause,
      });
      if (cause instanceof HttpError && cause.status === 409) {
        const message = cause.code === "WORKSPACE_BUSY"
          ? "工作区正在处理其他操作，请稍后重试。"
          : "这条回复还没结束，暂时不能分叉。";
        notify.error("Fork 失败", { error: new UserFacingError(message) });
      } else {
        notify.error("Fork 失败", {
          error: cause,
          fallback: "Fork 失败，请重试",
          action: {
            label: "重试",
            onClick: () => void executeFork(sourceSessionId, messageId, originLocationKey),
          },
        });
      }
    } finally {
      forkPending.current = false;
      setForkingMessageId(null);
    }
  }, [navigate]);

  const forkMessage = useCallback(async (messageId: string) => {
    if (focusedSessionId === null) return;
    await executeFork(focusedSessionId, messageId, location.key);
  }, [executeFork, focusedSessionId, location.key]);

  /**
   * 删除工作区或会话：确认框托管 busy 与失败提示（失败时保持打开可重试）。
   * 删除成功后才关闭工作台标签、离开已删除的当前页并刷新侧栏。
   *
   * 会话有 fork 后代时，确认框多一个「一起删除」：主操作仍是只删这一个（它的 fork 改挂到上一级），
   * 因为它是两者里更可挽回的那个。删掉了哪些 Session 以后端返回为准，前端只用 `forkCount` 询问。
   */
  const requestDelete = useCallback(async (target: DeleteTarget) => {
    const deletingCurrentWorkspace = target.kind === "workspace" && (
      selectedWorkspaceId === target.id
      || workspaces.some((workspace) => workspace.workspace_id === target.id && workspace.sessions.some((session) => session.session_id === focusedSessionId))
    );
    let deletedSessionIds: number[] = [];
    let deletedWithForks = false;
    const run = async (withForks: boolean) => {
      try {
        if (target.kind === "workspace") {
          await deleteWorkspace(target.id);
          deletedSessionIds = workspaces.find((workspace) => workspace.workspace_id === target.id)?.sessions.map((session) => session.session_id) ?? [];
        } else {
          deletedSessionIds = (await deleteSession(target.id, { withForks })).deleted_session_ids;
          deletedWithForks = withForks;
        }
      } catch (cause) {
        void frontendLog("ERROR", "workspace_resource_delete_failed", "删除工作区资源失败", {
          data: { kind: target.kind, id: target.id, withForks },
          error: cause,
        });
        throw cause;
      }
    };
    const forkCount = target.kind === "session" ? target.forkCount : 0;
    const confirmed = await confirmDelete({
      target: target.kind === "workspace" ? `工作区「${target.label}」` : `会话「${target.label}」`,
      consequence: target.kind === "workspace"
        ? `将删除该工作区及其下的 ${target.sessionCount} 个会话及其全部数据。`
        : forkCount > 0
          ? `这个会话有 ${forkCount} 个 fork 出来的会话。只删除这个时，它们会提升一级保留下来；也可以一起删除。`
          : "将删除该会话及其全部数据，不影响所属工作区和其他会话。",
      confirmLabel: forkCount > 0 ? "只删除这个" : "删除",
      action: () => run(false),
      alternative: forkCount > 0
        ? { label: `一起删除 ${forkCount + 1} 个会话`, action: () => run(true) }
        : undefined,
    });
    if (!confirmed) return;
    // 终端工作台、后台工作看板、对话行高缓存、工作台标签与这些 Session 的模型选择、置顶随 Session 删除一并丢弃（后端已清理对应事实）。
    useTerminalWorkbenchStore.getState().forget(deletedSessionIds);
    useWorkbenchStore.getState().forgetSessions(deletedSessionIds);
    useBackgroundTasksStore.getState().forget(deletedSessionIds);
    useHandoffStore.getState().forget(deletedSessionIds);
    forgetMessageHeights(deletedSessionIds);
    forgetCommands(deletedSessionIds);
    useModelSelectionStore.getState().forgetSessions(deletedSessionIds);
    usePinnedSessionsStore.getState().forget(deletedSessionIds);
    for (const sessionId of deletedSessionIds) {
      useDisclosureStore.getState().clearSession(sessionId);
      forgetSessionComposerDrafts(sessionId);
    }
    if (target.kind === "workspace") {
      useWorkbenchStore.getState().forgetWorkspace(target.id);
      useFilesStore.getState().forget(target.id);
      useModelSelectionStore.getState().forgetWorkspace(target.id);
      if (deletingCurrentWorkspace) {
        selectWorkspace(null);
        navigate("/");
      }
    } else if (focusedSessionId !== null && deletedSessionIds.includes(focusedSessionId)) {
      navigate("/");
    }
    notify.success(target.kind === "workspace" ? "工作区已删除" : deletedWithForks ? `已删除 ${forkCount + 1} 个会话` : "会话已删除");
  }, [focusedSessionId, navigate, selectWorkspace, selectedWorkspaceId, workspaces]);

  return { addWorkspaceFromFolder, addingWorkspace, forkMessage, forkingMessageId, requestDelete };
}
