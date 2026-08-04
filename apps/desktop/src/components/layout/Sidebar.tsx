/**
 * 左侧导航栏（Sidebar）。
 *
 * 展示工作区、任务树、日志入口和底部辅助入口。
 * 工作区与任务数据来自 Zustand store，选中任务通过 useTask 打开并加载 turn/event。
 *
 * @module components/layout/Sidebar
 */

import { useState } from "react";
import {
  FileText,
  Settings,
  User,
  Plus,
  ChevronRight,
  FolderOpen,
  Trash2,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Button } from "@/components/ui/button";
import { HistoryList } from "@/components/sidebar/HistoryList";
import { PluginList } from "@/components/sidebar/PluginList";
import { cn } from "@/lib/utils";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useTaskStore } from "@/stores/taskStore";
import { useEventStore } from "@/stores/eventStore";
import { useTask } from "@/hooks/useTask";
import * as api from "@/services/api";
import { deleteTask as deleteTaskApi } from "@/services/api";
import { logError } from "@/lib/logger";
import type { TaskRecord } from "@shared/task";
import type { WorkspaceRecord } from "@shared/workspace";
import appIconUrl from "../../../src-tauri/icons/icon.png";

/** Sidebar 组件属性。 */
export interface SidebarProps {
  /** 当前主视图，用于展示导航选中态。 */
  activeView: "chat" | "new-task" | "logs";
  /** 打开日志页面。 */
  onOpenLogs: () => void;
  /** 返回会话页面。 */
  onOpenChat: () => void;
  /** 打开新建任务页面。 */
  onNewTask: () => void;
}

/**
 * Sidebar 左侧导航栏组件。
 *
 * 宽度由外层可拖拽 Panel 决定（本组件撑满容器），负责工作区任务树导航、
 * 新建任务入口、日志入口和工作区删除。
 */
export function Sidebar({ activeView, onOpenLogs, onOpenChat, onNewTask }: SidebarProps) {
  const workspaces = useWorkspaceStore((s) => s.workspaces);
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  const collapsedWorkspaceIds = useWorkspaceStore((s) => s.collapsedWorkspaceIds);
  const setActiveWorkspace = useWorkspaceStore((s) => s.setActiveWorkspace);
  const toggleWorkspaceCollapsed = useWorkspaceStore((s) => s.toggleWorkspaceCollapsed);
  const removeWorkspace = useWorkspaceStore((s) => s.removeWorkspace);
  const tasks = useTaskStore((s) => s.tasks);
  const setTasks = useTaskStore((s) => s.setTasks);
  const removeTask = useTaskStore((s) => s.removeTask);
  const activeTaskId = useTaskStore((s) => s.activeTaskId);
  const { openTask } = useTask();

  // 待删除的工作区（非空时展示应用内确认弹窗）。
  const [pendingDelete, setPendingDelete] = useState<WorkspaceRecord | null>(null);
  // 待删除工作区下的任务数量（用于确认弹窗展示）。
  const pendingDeleteTaskCount =
    pendingDelete ? tasks.filter((task) => task.workspace_id === pendingDelete.workspace_id).length : 0;
  // 待删除工作区是否为当前活跃工作区（用于确认弹窗提示切换/新建行为）。
  const pendingDeleteIsActive = pendingDelete ? activeWorkspaceId === pendingDelete.workspace_id : false;
  // 删除请求进行中标记，用于禁用按钮并展示 loading 文案。
  const [deleting, setDeleting] = useState(false);
  // 删除失败提示，展示在确认弹窗内。
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // 待删除的任务（非空时展示确认弹窗）。
  const [pendingDeleteTask, setPendingDeleteTask] = useState<TaskRecord | null>(null);
  // 任务删除进行中标记。
  const [deletingTask, setDeletingTask] = useState(false);
  // 任务删除失败提示。
  const [deleteTaskError, setDeleteTaskError] = useState<string | null>(null);

  /**
   * 执行工作区删除。
   *
   * 调用后端删除接口，成功后同步移除本地工作区与其任务并关闭弹窗；
   * 删除当前活跃工作区时一并清空活跃任务并将主视图切回会话页，
   * 若列表已空（删除的是唯一工作区）则跳转到新建任务页引导选择工作区；
   * 失败时保留弹窗并在其中展示错误信息。
   */
  const handleConfirmDelete = async () => {
    if (!pendingDelete) {
      return;
    }
    const deletedWorkspaceId = pendingDelete.workspace_id;
    const wasActive = activeWorkspaceId === deletedWorkspaceId;
    setDeleting(true);
    setDeleteError(null);
    try {
      await api.deleteWorkspace(deletedWorkspaceId);
      const removedTaskIds = tasks
        .filter((task) => task.workspace_id === deletedWorkspaceId)
        .map((task) => task.task_id);
      // removeWorkspace 内部会在删除当前活跃区时自动切到剩余列表第一项。
      removeWorkspace(deletedWorkspaceId);
      setTasks(tasks.filter((task) => task.workspace_id !== deletedWorkspaceId));
      // 同步使被删工作区下各任务的事件缓存失效，避免幽灵 timeline。
      for (const taskId of removedTaskIds) {
        useEventStore.getState().invalidateTask(taskId);
      }
      setPendingDelete(null);
      // 删的是当前活跃区 → 主视图切回会话页，避免停留在已不存在的对话。
      if (wasActive) {
        onOpenChat();
      }
    } catch (err) {
      logError("删除工作区失败", err, { module: "Sidebar", workspace_id: deletedWorkspaceId });
      setDeleteError(err instanceof Error ? err.message : "删除工作区失败，请检查后端日志");
    } finally {
      setDeleting(false);
    }
    // 删除态已结束后再跳转：删完已无工作区（删的是唯一区）时跳新建任务页引导选择工作区，
    // 不在此直接弹系统目录选择器（避免在 deleting 态内持有长耗时交互）。
    if (useWorkspaceStore.getState().workspaces.length === 0) {
      onNewTask();
    }
  };

  /**
   * 执行单个任务删除。
   *
   * 调用后端删除接口并级联清理其下轮次 / 事件；成功后同步移除本地任务与其事件缓存。
   */
  const handleConfirmDeleteTask = async () => {
    if (!pendingDeleteTask) {
      return;
    }
    setDeletingTask(true);
    setDeleteTaskError(null);
    try {
      await deleteTaskApi(pendingDeleteTask.task_id);
      // removeTask 内部已同步使该任务的事件缓存失效（见 taskStore）。
      removeTask(pendingDeleteTask.task_id);
      setPendingDeleteTask(null);
    } catch (err) {
      logError("删除任务失败", err, { module: "Sidebar", task_id: pendingDeleteTask.task_id });
      setDeleteTaskError(err instanceof Error ? err.message : "删除任务失败，请检查后端日志");
    } finally {
      setDeletingTask(false);
    }
  };

  return (
    <aside className="flex h-full w-full min-w-0 flex-col bg-sidebar text-sidebar-foreground">
      {/* 应用标题 */}
      <div className="flex h-12 items-center gap-2 px-4 font-semibold tracking-tight">
        <img src={appIconUrl} alt="Coding Agent" className="h-6 w-6 rounded-sm object-contain" />
        <span>Coding Agent</span>
      </div>

      <Separator />

      {/* 主内容区域：可滚动 */}
      <ScrollArea className="flex-1 scrollbar-thin">
        {/* 新建任务按钮 */}
        <div className="p-2">
          <Button
            variant="outline"
            className="w-full justify-start gap-2 text-sm"
            onClick={activeView === "logs" ? onOpenChat : onNewTask}
            disabled={activeView === "new-task"}
          >
            <Plus className="h-4 w-4" />
            {activeView === "logs" ? "返回会话" : "新建任务"}
          </Button>
        </div>

        <div className="px-2 pb-2">
          <Button
            variant={activeView === "logs" ? "secondary" : "ghost"}
            className="w-full justify-start gap-2 text-sm"
            onClick={onOpenLogs}
          >
            <FileText className="h-4 w-4" />
            日志
          </Button>
        </div>

        <div className="px-2 py-1">
          <div className="mb-1 flex items-center gap-1 px-2 py-1.5 text-xs font-medium uppercase text-muted-foreground">
            <FolderOpen className="h-3.5 w-3.5" />
            工作区
          </div>
          {workspaces.map((workspace) => {
            const collapsed = collapsedWorkspaceIds.has(workspace.workspace_id);
            const workspaceTasks = tasks.filter((task) => task.workspace_id === workspace.workspace_id);
            return (
              <div key={workspace.workspace_id}>
                <div
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors",
                    activeWorkspaceId === workspace.workspace_id
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:bg-accent/50",
                  )}
                >
                  <button
                    onClick={() => {
                      setActiveWorkspace(workspace.workspace_id);
                      toggleWorkspaceCollapsed(workspace.workspace_id);
                    }}
                    title={`${workspace.name} (${workspace.root_path})`}
                    className="flex min-w-0 flex-1 items-center gap-2 text-left"
                  >
                    <ChevronRight className={cn("h-3.5 w-3.5 shrink-0 transition-transform", !collapsed && "rotate-90")} />
                    <span className="truncate">{workspace.name}</span>
                  </button>
                  <button
                    title="删除工作区"
                    className="ml-auto rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                    onClick={(event) => {
                      event.stopPropagation();
                      setDeleteError(null);
                      setPendingDelete(workspace);
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
                {!collapsed && (
                  <div className="ml-5 mt-1 space-y-1">
                    {workspaceTasks.map((task) => (
                      <div
                        key={task.task_id}
                        className="flex w-full items-center gap-1 rounded-md px-2 py-1.5 transition-colors hover:bg-accent/50"
                      >
                        <button
                          onClick={() => {
                            onOpenChat();
                            void openTask(task.task_id);
                          }}
                          title={task.title}
                          className={cn(
                            "flex min-w-0 flex-1 items-center gap-2 text-left text-xs transition-colors",
                            activeTaskId === task.task_id
                              ? "bg-accent text-accent-foreground"
                              : "text-muted-foreground",
                          )}
                        >
                          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-current opacity-60" />
                          <span className="truncate">{task.title || task.last_message_preview}</span>
                        </button>
                        <button
                          title="删除任务"
                          className="ml-auto rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                          onClick={(event) => {
                            event.stopPropagation();
                            setDeleteTaskError(null);
                            setPendingDeleteTask(task);
                          }}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <Separator className="my-2" />

        {/* 历史会话入口 — 使用独立子组件 */}
        <HistoryList />

        {/* 插件入口 — 使用独立子组件 */}
        <PluginList />
      </ScrollArea>

      <Separator />

      {/* 底部用户 / 设置入口（占位） */}
      <div className="flex items-center gap-2 p-2">
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-muted text-xs font-medium">
          <User className="h-4 w-4" />
        </div>
        <span className="flex-1 truncate text-sm">Lucky/Dog</span>
        <Button variant="ghost" size="icon" className="h-7 w-7">
          <Settings className="h-4 w-4" />
        </Button>
      </div>

      {/* 删除工作区确认弹窗（应用内实现，不依赖系统 dialog） */}
      {pendingDelete && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onClick={() => {
            if (!deleting) {
              setPendingDelete(null);
            }
          }}
        >
          <div
            className="w-80 rounded-lg border border-border bg-background p-4 shadow-lg"
            onClick={(event) => event.stopPropagation()}
          >
            <h3 className="text-sm font-semibold text-foreground">删除工作区</h3>
            <p className="mt-2 text-sm text-muted-foreground">
              确定删除工作区「{pendingDelete.name}」及其 {pendingDeleteTaskCount} 个任务记录？此操作不可恢复。
              {pendingDeleteIsActive &&
                (workspaces.length > 1
                  ? "删除后自动切换到其他工作区。"
                  : "删除后将跳转到新建任务页选择工作区。")}
            </p>
            {deleteError && <p className="mt-2 text-xs text-destructive">{deleteError}</p>}
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={deleting}
                onClick={() => setPendingDelete(null)}
              >
                取消
              </Button>
              <Button
                variant="destructive"
                size="sm"
                disabled={deleting}
                onClick={() => void handleConfirmDelete()}
              >
                {deleting ? "删除中…" : "删除"}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* 删除任务确认弹窗（应用内实现，不依赖系统 dialog） */}
      {pendingDeleteTask && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          onClick={() => {
            if (!deletingTask) {
              setPendingDeleteTask(null);
            }
          }}
        >
          <div
            className="w-80 rounded-lg border border-border bg-background p-4 shadow-lg"
            onClick={(event) => event.stopPropagation()}
          >
            <h3 className="text-sm font-semibold text-foreground">删除任务</h3>
            <p className="mt-2 text-sm text-muted-foreground">
              确定删除任务「{pendingDeleteTask.title || pendingDeleteTask.last_message_preview}」及其对话记录？此操作不可恢复。
            </p>
            {deleteTaskError && <p className="mt-2 text-xs text-destructive">{deleteTaskError}</p>}
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={deletingTask}
                onClick={() => setPendingDeleteTask(null)}
              >
                取消
              </Button>
              <Button
                variant="destructive"
                size="sm"
                disabled={deletingTask}
                onClick={() => void handleConfirmDeleteTask()}
              >
                {deletingTask ? "删除中…" : "删除"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
