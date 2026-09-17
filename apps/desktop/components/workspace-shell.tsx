"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  FolderIcon,
  GitForkIcon,
  Loader2Icon,
  MessageSquarePlusIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  RefreshCwIcon,
} from "lucide-react";

import { TaskPage } from "@/components/task-page";
import { Button } from "@/components/ui/button";
import { NewConversation } from "@/components/new-conversation";
import type { InitialConversationAttachment } from "@/components/new-conversation";
import { DeleteConfirmDialog } from "@/components/delete-confirm-dialog";
import { ResourceActionMenu } from "@/components/resource-action-menu";
import { TaskTree } from "@/components/task-tree/task-tree";
import { BackendStatusBanner } from "@/components/backend-status-banner";
import { Workbench } from "@/components/workbench";
import { WorkbenchProvider } from "@/lib/workbench/context";
import { useWorkbenchStore } from "@/lib/workbench/store";
import {
  deleteTask,
  deleteWorkspace,
  forkTask,
  getWorkspaceTasks,
  getWorkspaces,
  type Workspace,
  type WorkspaceTask,
} from "@/lib/api/workspaces";
import { HttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { readLastWorkspaceId, writeLastWorkspaceId } from "@/lib/workspace-preferences";

type WorkspaceWithTasks = Workspace & { tasks: WorkspaceTask[]; taskLoadError?: string };
type DeleteTarget =
  | { kind: "workspace"; id: number; label: string; taskCount: number }
  | { kind: "task"; id: number; label: string; taskCount: number }
  | null;

const NARROW_VIEWPORT_QUERY = "(max-width: 1024px)";

export function WorkspaceShell({ routeTaskId, initialMessage, initialAttachments }: { routeTaskId: number | null; initialMessage?: string; initialAttachments?: InitialConversationAttachment[] }) {
  const navigate = useNavigate();
  const closeWorkspaceTabs = useWorkbenchStore((state) => state.closeWorkspace);
  const [collapsed, setCollapsed] = useState(() => (
    typeof window !== "undefined" && window.matchMedia(NARROW_VIEWPORT_QUERY).matches
  ));
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<number | null>(null);
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<number[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceWithTasks[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [forkingRunId, setForkingRunId] = useState<number | null>(null);
  const [forkError, setForkError] = useState<string | null>(null);
  const loadGenerationRef = useRef(0);
  const loadControllerRef = useRef<AbortController | null>(null);
  const catalogLoadingRef = useRef(false);
  const taskRefreshGenerationRef = useRef(new Map<number, number>());
  const taskRefreshControllerRef = useRef(new Map<number, AbortController>());
  const pendingTaskRefreshRef = useRef(new Set<number>());
  const [routeTask, setRouteTask] = useState<WorkspaceTask | null>(null);

  const load = useCallback(async () => {
    const generation = ++loadGenerationRef.current;
    catalogLoadingRef.current = true;
    void frontendLog("DEBUG", "workspace_catalog_load_started", "Workspace catalog 开始加载", {
      data: { generation, reason: "explicit_or_initial" },
    });
    loadControllerRef.current?.abort();
    for (const [workspaceId, controller] of taskRefreshControllerRef.current.entries()) {
      pendingTaskRefreshRef.current.add(workspaceId);
      controller.abort();
    }
    const controller = new AbortController();
    loadControllerRef.current = controller;
    setStatus("loading");
    setError(null);
    try {
      const baseWorkspaces = await getWorkspaces({ signal: controller.signal });
      const withTasks = await Promise.all(baseWorkspaces.map(async (workspace) => {
        try {
          return { ...workspace, tasks: await getWorkspaceTasks(workspace.workspace_id, { signal: controller.signal }) };
        } catch (cause: unknown) {
          if (controller.signal.aborted) throw cause;
          return {
            ...workspace,
            tasks: [],
            taskLoadError: cause instanceof Error ? cause.message : "任务列表加载失败",
          };
        }
      }));
      if (generation !== loadGenerationRef.current || controller.signal.aborted) {
        void frontendLog("DEBUG", "workspace_catalog_response_discarded", "Workspace catalog 丢弃过期响应", {
          data: { generation, currentGeneration: loadGenerationRef.current, aborted: controller.signal.aborted },
        });
        return;
      }
      setWorkspaces(withTasks);
      setSelectedWorkspaceId((current) => {
        if (current && withTasks.some((workspace) => workspace.workspace_id === current)) return current;
        const lastWorkspaceId = readLastWorkspaceId();
        return lastWorkspaceId && withTasks.some((workspace) => workspace.workspace_id === lastWorkspaceId)
          ? lastWorkspaceId
          : null;
      });
      setExpandedWorkspaceIds((current) => current.length > 0 ? current.filter((id) => withTasks.some((workspace) => workspace.workspace_id === id)) : withTasks.map((workspace) => workspace.workspace_id));
      setStatus("ready");
      void frontendLog("DEBUG", "workspace_catalog_loaded", "Workspace catalog 加载完成", {
        data: { generation, workspaceCount: withTasks.length, taskCount: withTasks.reduce((count, workspace) => count + workspace.tasks.length, 0) },
      });
    } catch (cause) {
      if (generation !== loadGenerationRef.current || controller.signal.aborted) return;
      setStatus("error");
      setError(cause instanceof Error ? cause.message : "工作区加载失败，请重试");
      void frontendLog("WARNING", "workspace_catalog_load_failed", "Workspace catalog 加载失败", {
        data: { generation },
        error: cause,
      });
    } finally {
      if (generation === loadGenerationRef.current) catalogLoadingRef.current = false;
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      loadGenerationRef.current += 1;
      loadControllerRef.current?.abort();
      for (const controller of taskRefreshControllerRef.current.values()) controller.abort();
    };
  }, [load]);

  useEffect(() => {
    const mediaQuery = window.matchMedia(NARROW_VIEWPORT_QUERY);
    const syncCollapsedState = () => setCollapsed(mediaQuery.matches);
    syncCollapsedState();
    mediaQuery.addEventListener("change", syncCollapsedState);
    return () => mediaQuery.removeEventListener("change", syncCollapsedState);
  }, []);

  useEffect(() => {
    if (routeTaskId === null) {
      setRouteTask(null);
      return;
    }
    const workspace = workspaces.find((candidate) => candidate.tasks.some((task) => task.task_id === routeTaskId));
    const task = workspace?.tasks.find((candidate) => candidate.task_id === routeTaskId);
    if (task) {
      setRouteTask(task);
      setSelectedWorkspaceId(task.workspace_id);
      writeLastWorkspaceId(task.workspace_id);
      return;
    }
    // Keep a task already confirmed by GET /tasks/:id while a sidebar refresh
    // temporarily returns an incomplete list. A new route still drops the
    // previous task because its id no longer matches the URL.
    setRouteTask((current) => current?.task_id === routeTaskId ? current : null);
  }, [routeTaskId, workspaces]);

  useEffect(() => {
    void frontendLog("DEBUG", "workspace_route_changed", "WorkspaceShell 观察到路由 task 变化", {
      data: { routeTaskId },
    });
  }, [routeTaskId]);

  const selectedWorkspace = useMemo(() => workspaces.find((workspace) => workspace.workspace_id === selectedWorkspaceId), [selectedWorkspaceId, workspaces]);
  const activeTaskId = routeTaskId;
  const activeTask = useMemo(
    () => workspaces.flatMap((workspace) => workspace.tasks).find((task) => task.task_id === activeTaskId)
      ?? (routeTask?.task_id === activeTaskId ? routeTask : undefined),
    [activeTaskId, routeTask, workspaces],
  );
  const activeWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.workspace_id === (activeTask?.workspace_id ?? selectedWorkspaceId)),
    [activeTask?.workspace_id, selectedWorkspaceId, workspaces],
  );
  const activeTaskWorkspaceId = useMemo(() => {
    if (activeTaskId === null) return selectedWorkspaceId;
    if (routeTask?.task_id === activeTaskId) return routeTask.workspace_id;
    return workspaces.find((workspace) => workspace.tasks.some((task) => task.task_id === activeTaskId))?.workspace_id
      ?? null;
  }, [activeTaskId, routeTask, selectedWorkspaceId, workspaces]);
  const refreshWorkspaceTasks = useCallback(async (workspaceId: number) => {
    if (catalogLoadingRef.current) {
      pendingTaskRefreshRef.current.add(workspaceId);
      void frontendLog("DEBUG", "workspace_task_refresh_queued", "全量目录加载期间排队 workspace task 刷新", {
        data: { workspaceId, pendingCount: pendingTaskRefreshRef.current.size },
      });
      return;
    }
    const nextGeneration = (taskRefreshGenerationRef.current.get(workspaceId) ?? 0) + 1;
    taskRefreshGenerationRef.current.set(workspaceId, nextGeneration);
    taskRefreshControllerRef.current.get(workspaceId)?.abort();
    const controller = new AbortController();
    taskRefreshControllerRef.current.set(workspaceId, controller);
    const catalogGeneration = loadGenerationRef.current;
    void frontendLog("DEBUG", "workspace_task_refresh_started", "Workspace task 列表开始定向刷新", {
      data: { workspaceId, requestGeneration: nextGeneration, catalogGeneration },
    });
    try {
      const tasks = await getWorkspaceTasks(workspaceId, { signal: controller.signal });
      if (
        controller.signal.aborted
        || catalogGeneration !== loadGenerationRef.current
        || nextGeneration !== taskRefreshGenerationRef.current.get(workspaceId)
      ) return;
      setWorkspaces((current) => current.map((workspace) => workspace.workspace_id === workspaceId
        ? { ...workspace, tasks, taskLoadError: undefined }
        : workspace));
      void frontendLog("DEBUG", "workspace_task_refresh_completed", "Workspace task 列表定向刷新完成", {
        data: { workspaceId, requestGeneration: nextGeneration, taskCount: tasks.length },
      });
    } catch (cause: unknown) {
      if (controller.signal.aborted) return;
      if (catalogGeneration !== loadGenerationRef.current || nextGeneration !== taskRefreshGenerationRef.current.get(workspaceId)) return;
      setWorkspaces((current) => current.map((workspace) => workspace.workspace_id === workspaceId
        ? { ...workspace, taskLoadError: cause instanceof Error ? cause.message : "任务列表加载失败" }
        : workspace));
      void frontendLog("WARNING", "workspace_task_refresh_failed", "Workspace task 列表定向刷新失败", {
        data: { workspaceId, requestGeneration: nextGeneration },
        error: cause,
      });
    } finally {
      if (taskRefreshControllerRef.current.get(workspaceId) === controller) {
        taskRefreshControllerRef.current.delete(workspaceId);
      }
    }
  }, []);
  useEffect(() => {
    if (status === "loading" || pendingTaskRefreshRef.current.size === 0) return;
    const pendingWorkspaceIds = [...pendingTaskRefreshRef.current];
    pendingTaskRefreshRef.current.clear();
    for (const workspaceId of pendingWorkspaceIds) void refreshWorkspaceTasks(workspaceId);
  }, [refreshWorkspaceTasks, status]);
  const handleTaskLoaded = useCallback((task: WorkspaceTask) => {
    if (task.task_id !== routeTaskId) return;
    setRouteTask(task);
    setSelectedWorkspaceId(task.workspace_id);
    writeLastWorkspaceId(task.workspace_id);
  }, [routeTaskId]);
  const toggleWorkspace = (workspaceId: number) => setExpandedWorkspaceIds((current) => current.includes(workspaceId) ? current.filter((id) => id !== workspaceId) : [...current, workspaceId]);
  const selectWorkspace = (workspaceId: number) => { setSelectedWorkspaceId(workspaceId); writeLastWorkspaceId(workspaceId); };
  const startNewConversation = (workspaceId = selectedWorkspaceId) => {
    if (workspaceId) writeLastWorkspaceId(workspaceId);
    setSelectedWorkspaceId(workspaceId);
    navigate("/");
  };
  const requestDelete = (target: NonNullable<DeleteTarget>) => { setDeleteError(null); setDeleteTarget(target); };
  const refreshTaskState = useCallback(() => {
    const workspaceId = activeTaskWorkspaceId;
    if (workspaceId !== null) void refreshWorkspaceTasks(workspaceId);
  }, [activeTaskWorkspaceId, refreshWorkspaceTasks]);
  const handleRunStateChange = useCallback((isRunning: boolean) => {
    if (activeTaskId === null) return;
    setWorkspaces((current) => current.map((workspace) => ({
      ...workspace,
      tasks: workspace.tasks.map((task) => task.task_id === activeTaskId
        ? { ...task, fork_available: !isRunning }
        : task),
    })));
  }, [activeTaskId]);
  const handleForkRun = useCallback(async (runId: number) => {
    if (activeTaskId === null || forkingRunId !== null) return;
    setForkError(null);
    setForkingRunId(runId);
    try {
      const target = await forkTask(activeTaskId, runId);
      await load();
      navigate(`/tasks/${target.task_id}`);
    } catch (cause) {
      if (cause instanceof HttpError && cause.status === 409) {
        setForkError(cause.code === "SNAPSHOT_NOT_READY"
          ? "对话快照仍在准备中，请稍后重试。"
          : "当前 Task 仍有运行中的 Run，暂时不能 Fork。"
        );
        await load();
      } else {
        setForkError(cause instanceof Error ? cause.message : "Fork 失败，请重试");
      }
    } finally {
      setForkingRunId(null);
    }
  }, [activeTaskId, forkingRunId, load, navigate]);

  const confirmDelete = async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      if (deleteTarget.kind === "workspace") {
        const deletingCurrentWorkspace = selectedWorkspaceId === deleteTarget.id || workspaces.some((workspace) => workspace.workspace_id === deleteTarget.id && workspace.tasks.some((task) => task.task_id === activeTaskId));
        await deleteWorkspace(deleteTarget.id);
        closeWorkspaceTabs(deleteTarget.id);
        if (deletingCurrentWorkspace) {
          setSelectedWorkspaceId(null);
          navigate("/");
        }
      } else {
        await deleteTask(deleteTarget.id);
        if (activeTaskId === deleteTarget.id) {
          navigate("/");
        }
      }
      setDeleteTarget(null);
      await load();
    } catch (cause) {
      setDeleteError(cause instanceof Error ? cause.message : "删除失败，请重试");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <WorkbenchProvider workspaceId={activeTaskWorkspaceId}>
    <div className="bg-background flex h-dvh min-h-0 overflow-hidden">
      <BackendStatusBanner />
      <aside className={`bg-muted/20 flex min-h-0 shrink-0 flex-col border-r transition-[width] duration-200 max-[1024px]:transition-none ${collapsed ? "w-14" : "w-72"}`}>
        <div className="flex h-14 items-center justify-between border-b px-3">
          {!collapsed && <span className="text-sm font-semibold">工作区</span>}
          <Button variant="ghost" size="icon-sm" onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? "展开侧栏" : "收起侧栏"}>
            {collapsed ? <PanelLeftOpenIcon /> : <PanelLeftCloseIcon />}
          </Button>
        </div>
        {!collapsed && (
          <>
            <div className="flex gap-2 p-3">
              <Button className="flex-1 justify-start gap-2" onClick={() => startNewConversation()}><MessageSquarePlusIcon className="size-4" />新对话</Button>
              <Button variant="outline" size="icon" onClick={() => void load()} disabled={status === "loading"} aria-label="刷新工作区"><RefreshCwIcon className={status === "loading" ? "animate-spin" : ""} /></Button>
            </div>
            <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-4" aria-label="工作区任务列表">
              <p className="text-muted-foreground px-2 pb-2 text-xs font-medium">我的工作区</p>
              {status === "loading" && <Loader2Icon className="text-muted-foreground mx-2 size-4 animate-spin" />}
              {status === "error" && <div className="px-2 text-xs"><p className="text-destructive mb-2">{error}</p><Button variant="outline" size="sm" onClick={() => void load()}>重试</Button></div>}
              {status === "ready" && workspaces.length === 0 && <p className="text-muted-foreground px-2 text-xs">暂无工作区</p>}
              <div className="space-y-1">
                {workspaces.map((workspace) => {
                  const expanded = expandedWorkspaceIds.includes(workspace.workspace_id);
                  return (
                    <section key={workspace.workspace_id}>
                      <div className={`group flex items-center rounded-md ${selectedWorkspaceId === workspace.workspace_id && !activeTaskId ? "bg-muted" : "hover:bg-muted"}`}>
                          <button type="button" className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left text-sm" onClick={() => { selectWorkspace(workspace.workspace_id); toggleWorkspace(workspace.workspace_id); }}>
                          {expanded ? <ChevronDownIcon className="size-3.5 shrink-0" /> : <ChevronRightIcon className="size-3.5 shrink-0" />}
                          <FolderIcon className="text-muted-foreground size-4 shrink-0" /><span className="truncate">{workspace.name}</span>{workspace.taskLoadError ? <span className="text-destructive ml-auto text-xs" title={workspace.taskLoadError}>加载失败</span> : <span className="text-muted-foreground ml-auto text-xs">{workspace.tasks.length}</span>}
                        </button>
                        <ResourceActionMenu label={workspace.name} onDelete={() => requestDelete({ kind: "workspace", id: workspace.workspace_id, label: workspace.name, taskCount: workspace.tasks.length })} />
                      </div>
                      {expanded && <div className="ml-5 border-l pl-2">
                        <TaskTree
                          tasks={workspace.tasks}
                          activeTaskId={activeTaskId}
                          onSelectTask={(task) => { setForkError(null); writeLastWorkspaceId(workspace.workspace_id); setSelectedWorkspaceId(workspace.workspace_id); navigate(`/tasks/${task.task_id}`); }}
                          renderActions={(task) => <ResourceActionMenu label={task.full_title} onDelete={() => requestDelete({ kind: "task", id: task.task_id, label: task.full_title, taskCount: 0 })} />}
                        />
                      </div>}
                    </section>
                  );
                })}
              </div>
            </nav>
          </>
        )}
      </aside>
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex h-14 shrink-0 items-center justify-between border-b px-5"><div className="min-w-0"><p className="flex items-center gap-2 text-sm font-medium">{activeTaskId ? "对话" : "新对话"}{activeTask?.task_type === "fork" && <GitForkIcon className="text-muted-foreground size-3.5" aria-label="Fork Task" />}</p><p className="text-muted-foreground truncate text-xs">{forkError ?? (activeTaskId ? "已存在任务" : "选择工作区后开始创建对话")}</p></div>{selectedWorkspace && <div className="text-muted-foreground flex items-center gap-2 text-xs"><FolderIcon className="size-3.5" />{selectedWorkspace.name}</div>}</div>
        <div className="min-h-0 flex-1 overflow-hidden">
          {activeTaskId ? <TaskPage
            key={activeTaskId}
            taskId={activeTaskId}
            initialTask={activeTask}
            initialMessage={initialMessage}
            initialAttachments={initialAttachments}
            workspaceRoot={activeWorkspace?.root_path}
            forkAvailable={activeTask?.fork_available}
            forkingRunId={forkingRunId}
            onForkRun={(runId) => void handleForkRun(runId)}
            onTaskLoaded={handleTaskLoaded}
            onTaskStateChanged={refreshTaskState}
            onRunStateChange={handleRunStateChange}
          /> : <NewConversation workspaces={workspaces} selectedWorkspaceId={selectedWorkspaceId} onWorkspaceChange={(id) => { setSelectedWorkspaceId(id); writeLastWorkspaceId(id); }} onWorkspaceCreated={load} onStarted={(conversation, initialText, attachments) => { navigate(`/tasks/${conversation.task_id}`, { state: { initialMessage: initialText, initialAttachments: attachments } }); void load(); }} />}
        </div>
      </main>
      <Workbench workspaceId={activeTaskWorkspaceId} />
      {deleteTarget && <DeleteConfirmDialog open title={deleteTarget.kind === "workspace" ? `删除工作区“${deleteTarget.label}”？` : `删除任务“${deleteTarget.label}”？`} description={deleteTarget.kind === "workspace" ? `此操作将永久删除该工作区及其下的 ${deleteTarget.taskCount} 个任务和全部对话数据。` : "此操作将永久删除该任务及其全部对话数据，不影响所属工作区和其他任务。"} warning="删除后无法撤销。" error={deleteError} busy={deleting} onOpenChange={(open) => { if (!open) { setDeleteTarget(null); setDeleteError(null); } }} onConfirm={() => void confirmDelete()} />}
    </div>
    </WorkbenchProvider>
  );
}
