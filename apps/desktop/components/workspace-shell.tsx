"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  MessageSquarePlusIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  PanelRightCloseIcon,
  PanelRightOpenIcon,
  RefreshCwIcon,
  SearchIcon,
  Settings2Icon,
} from "lucide-react";

import { TaskPage } from "@/components/task-page";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import { NewConversation } from "@/components/new-conversation";
import type { InitialConversationAttachment } from "@/components/new-conversation";
import { DeleteConfirmDialog } from "@/components/delete-confirm-dialog";
import { ResourceActionMenu } from "@/components/resource-action-menu";
import { WorkspaceTree } from "@/components/task-tree/workspace-tree";
import type { WorkspaceTreeSource } from "@/components/task-tree/workspace-tree-model";
import { BackendStatusBanner } from "@/components/backend-status-banner";
import { BackendRestartButton } from "@/components/backend-restart-button";
import { Workbench } from "@/components/workbench";
import { WorkbenchProvider } from "@/lib/workbench/context";
import { SystemConfigurationPage } from "@/components/system-configuration-page";
import { WorkspaceConfigurationPage } from "@/components/workspace-configuration-page";
import { ContextObserverPage } from "@/components/context-observer-page";
import { useWorkbenchStore } from "@/lib/workbench/store";
import {
  deleteTask,
  deleteWorkspace,
  forkTask,
  getWorkspaceTasks,
  getWorkspaces,
  type StartedConversation,
  type WorkspaceTask,
} from "@/lib/api/workspaces";
import { HttpError } from "@/lib/http/errors";
import { frontendLog } from "@/lib/logging/frontend-log";
import { readLastWorkspaceId, writeLastWorkspaceId } from "@/lib/workspace-preferences";

type WorkspaceWithTasks = WorkspaceTreeSource;
type DeleteTarget =
  | { kind: "workspace"; id: number; label: string; taskCount: number }
  | { kind: "task"; id: number; label: string; taskCount: number }
  | null;

const NARROW_VIEWPORT_QUERY = "(max-width: 1024px)";

export function WorkspaceShell({ routeTaskId, initialMessage, initialAttachments, initialDisabledToolGroups, initialBanTools, settingsOpen = false, contextObserverOpen = false, workspaceConfigurationId = null }: {
  routeTaskId: number | null;
  initialMessage?: string;
  initialAttachments?: InitialConversationAttachment[];
  initialDisabledToolGroups?: string[];
  initialBanTools?: string[];
  settingsOpen?: boolean;
  contextObserverOpen?: boolean;
  workspaceConfigurationId?: number | null;
}) {
  const navigate = useNavigate();
  const closeWorkspaceTabs = useWorkbenchStore((state) => state.closeWorkspace);
  const workbenchTabs = useWorkbenchStore((state) => state.tabs);
  const workbenchPanelOpen = useWorkbenchStore((state) => state.panelOpen);
  const setWorkbenchPanelOpen = useWorkbenchStore((state) => state.setPanelOpen);
  const [collapsed, setCollapsed] = useState(() => (
    typeof window !== "undefined" && window.matchMedia(NARROW_VIEWPORT_QUERY).matches
  ));
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<number | null>(null);
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<number[] | null>(null);
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
  const knownWorkspaceIdsRef = useRef(new Set<number>());
  const catalogLoadingRef = useRef(false);
  const taskRefreshGenerationRef = useRef(new Map<number, number>());
  const taskRefreshControllerRef = useRef(new Map<number, AbortController>());
  const pendingTaskRefreshRef = useRef(new Set<number>());
  const [routeTask, setRouteTask] = useState<WorkspaceTask | null>(null);

  const hasUnsavedWorkbenchDraft = workbenchTabs.some(
    (tab) => tab.kind === "agent-configuration-draft" && tab.dirty,
  );
  const requestWorkbenchPanelClose = useCallback(() => {
    if (hasUnsavedWorkbenchDraft && !window.confirm("有未保存的配置草稿，确定收起工作台吗？")) return;
    setWorkbenchPanelOpen(false);
  }, [hasUnsavedWorkbenchDraft, setWorkbenchPanelOpen]);
  const toggleWorkbenchPanel = useCallback(() => {
    if (workbenchPanelOpen) {
      requestWorkbenchPanelClose();
      return;
    }
    setWorkbenchPanelOpen(true);
  }, [requestWorkbenchPanelClose, setWorkbenchPanelOpen, workbenchPanelOpen]);

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
      const receivedWorkspaceIds = new Set(withTasks.map((workspace) => workspace.workspace_id));
      setSelectedWorkspaceId((current) => {
        if (current && withTasks.some((workspace) => workspace.workspace_id === current)) return current;
        const lastWorkspaceId = readLastWorkspaceId();
        return lastWorkspaceId && withTasks.some((workspace) => workspace.workspace_id === lastWorkspaceId)
          ? lastWorkspaceId
          : null;
      });
      setExpandedWorkspaceIds((current) => {
        if (current === null) return withTasks.map((workspace) => workspace.workspace_id);
        const next = current.filter((id) => receivedWorkspaceIds.has(id));
        for (const workspace of withTasks) {
          if (!knownWorkspaceIdsRef.current.has(workspace.workspace_id)) next.push(workspace.workspace_id);
        }
        return next;
      });
      knownWorkspaceIdsRef.current = receivedWorkspaceIds;
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

  const startConversation = useCallback((conversation: StartedConversation, disabledToolGroups: string[]) => {
    navigate(`/tasks/${conversation.task_id}`, { state: { initialDisabledToolGroups: disabledToolGroups } });
    void load();
  }, [load, navigate]);

  useEffect(() => {
    void load();
    return () => {
      loadGenerationRef.current += 1;
      loadControllerRef.current?.abort();
      for (const controller of taskRefreshControllerRef.current.values()) controller.abort();
    };
  }, [load]);

  useEffect(() => {
    if (workspaceConfigurationId === null) return;
    setSelectedWorkspaceId(workspaceConfigurationId);
    writeLastWorkspaceId(workspaceConfigurationId);
  }, [workspaceConfigurationId]);

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
  const updateWorkspaceExpansion = (workspaceId: number, expanded: boolean) => {
    setExpandedWorkspaceIds((current) => {
      const ids = current ?? [];
      if (expanded) return ids.includes(workspaceId) ? ids : [...ids, workspaceId];
      return ids.filter((id) => id !== workspaceId);
    });
  };
  const selectWorkspace = (workspaceId: number) => { setSelectedWorkspaceId(workspaceId); writeLastWorkspaceId(workspaceId); };
  const startNewConversation = (workspaceId = selectedWorkspaceId) => {
    if (workspaceId) writeLastWorkspaceId(workspaceId);
    setSelectedWorkspaceId(workspaceId);
    navigate("/");
  };
  const openSettings = () => {
    navigate("/settings", { state: { returnTaskId: activeTaskId } });
  };
  const openWorkspaceSettings = (workspaceId: number) => {
    navigate(`/workspaces/${workspaceId}/settings`, { state: { returnTaskId: activeTaskId } });
  };
  const closeSettings = () => {
    navigate(activeTaskId ? `/tasks/${activeTaskId}` : "/");
  };
  const closeWorkspaceSettings = () => {
    navigate(activeTaskId ? `/tasks/${activeTaskId}` : "/");
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
              <Button variant="ghost" size="icon" onClick={() => void load()} disabled={status === "loading"} aria-label="刷新工作区"><RefreshCwIcon className={status === "loading" ? "animate-spin" : ""} /></Button>
            </div>
            <nav className="flex min-h-0 flex-1 flex-col px-2 pb-4" aria-label="工作区任务列表">
              <p className="shrink-0 px-2 pb-2 text-xs font-medium text-muted-foreground">我的工作区</p>
              {status === "loading" && <LoadingState size="compact" label="正在加载工作区…" className="px-2" />}
              {status === "error" && <ErrorState className="mx-2 text-xs" message={error} onRetry={() => void load()} />}
              {status === "ready" && workspaces.length === 0 && <EmptyState size="compact" title="暂无工作区" className="px-2" />}
              {workspaces.length > 0 && <div className="min-h-0 flex-1">
                <WorkspaceTree
                  workspaces={workspaces}
                  activeTaskId={activeTaskId}
                  selectedWorkspaceId={selectedWorkspaceId}
                  expandedWorkspaceIds={expandedWorkspaceIds}
                  onWorkspaceExpansionChange={updateWorkspaceExpansion}
                  onSelectWorkspace={selectWorkspace}
                  onOpenWorkspaceSettings={openWorkspaceSettings}
                  onSelectTask={(task, workspaceId) => { setForkError(null); writeLastWorkspaceId(workspaceId); setSelectedWorkspaceId(workspaceId); navigate(`/tasks/${task.task_id}`); }}
                  renderWorkspaceActions={(workspace) => <ResourceActionMenu label={workspace.name} onDelete={() => requestDelete({ kind: "workspace", id: workspace.workspace_id, label: workspace.name, taskCount: workspace.tasks.length })} />}
                  renderTaskActions={(task) => <ResourceActionMenu label={task.full_title} onDelete={() => requestDelete({ kind: "task", id: task.task_id, label: task.full_title, taskCount: 0 })} />}
                />
              </div>}
            </nav>
          </>
        )}
        <div className="mt-auto border-t p-3">
          <Button
            variant={contextObserverOpen ? "secondary" : "ghost"}
            className={`mb-1 w-full gap-2 ${collapsed ? "justify-center px-0" : "justify-start"}`}
            onClick={() => navigate("/context-observer")}
            aria-label="上下文观测"
            title={collapsed ? "上下文观测" : undefined}
          >
            <SearchIcon className="size-4" />
            {!collapsed && <span>上下文观测</span>}
          </Button>
          <Button
            variant="ghost"
            className={`w-full gap-2 ${collapsed ? "justify-center px-0" : "justify-start"}`}
            onClick={openSettings}
            aria-label="系统配置"
            title={collapsed ? "系统配置" : undefined}
          >
            <Settings2Icon className="size-4" />
            {!collapsed && <span>系统配置</span>}
          </Button>
        </div>
      </aside>
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex h-14 shrink-0 items-center justify-between border-b px-5">
          <div className="min-w-0">{forkError && <Alert tone="destructive" className="py-1 text-xs"><span className="block truncate">{forkError}</span></Alert>}</div>
          <div className="flex shrink-0 items-center gap-2">
            {workbenchTabs.length > 0 && (
              <Button
                type="button"
                variant={workbenchPanelOpen ? "secondary" : "outline"}
                size="sm"
                aria-label={`工作台，${workbenchTabs.length} 个标签，${workbenchPanelOpen ? "点击收起" : "点击打开"}`}
                aria-pressed={workbenchPanelOpen}
                title={workbenchPanelOpen ? "收起工作台" : "打开工作台"}
                onClick={toggleWorkbenchPanel}
              >
                {workbenchPanelOpen ? <PanelRightCloseIcon aria-hidden="true" /> : <PanelRightOpenIcon aria-hidden="true" />}
                <span className="max-[640px]:sr-only">工作台</span>
                <span className="text-muted-foreground min-w-4 text-center text-xs tabular-nums">{workbenchTabs.length}</span>
              </Button>
            )}
            <BackendRestartButton />
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden">
          {contextObserverOpen ? <ContextObserverPage /> : activeTaskId ? <TaskPage
            key={activeTaskId}
            taskId={activeTaskId}
            initialTask={activeTask}
            initialMessage={initialMessage}
            initialAttachments={initialAttachments}
            initialDisabledToolGroups={initialDisabledToolGroups}
            initialBanTools={initialBanTools}
            workspaceRoot={activeWorkspace?.root_path}
            forkAvailable={activeTask?.fork_available}
            forkingRunId={forkingRunId}
            onForkRun={(runId) => void handleForkRun(runId)}
            onTaskLoaded={handleTaskLoaded}
            onTaskStateChanged={refreshTaskState}
            onRunStateChange={handleRunStateChange}
          /> : <NewConversation workspaces={workspaces} selectedWorkspaceId={selectedWorkspaceId} onWorkspaceChange={(id) => { setSelectedWorkspaceId(id); writeLastWorkspaceId(id); }} onWorkspaceCreated={load} onStarted={startConversation} />}
        </div>
      </main>
      <Workbench workspaceId={activeTaskWorkspaceId} onRequestClose={requestWorkbenchPanelClose} />
      {deleteTarget && <DeleteConfirmDialog open title={deleteTarget.kind === "workspace" ? `删除工作区“${deleteTarget.label}”？` : `删除任务“${deleteTarget.label}”？`} description={deleteTarget.kind === "workspace" ? `此操作将永久删除该工作区及其下的 ${deleteTarget.taskCount} 个任务和全部对话数据。` : "此操作将永久删除该任务及其全部对话数据，不影响所属工作区和其他任务。"} warning="删除后无法撤销。" error={deleteError} busy={deleting} onOpenChange={(open) => { if (!open) { setDeleteTarget(null); setDeleteError(null); } }} onConfirm={() => void confirmDelete()} />}
      {settingsOpen && <SystemConfigurationPage onClose={closeSettings} />}
      {workspaceConfigurationId !== null && <WorkspaceConfigurationPage workspaceId={workspaceConfigurationId} workspaceName={selectedWorkspace?.name ?? "当前工作区"} onClose={closeWorkspaceSettings} />}
    </div>
    </WorkbenchProvider>
  );
}
