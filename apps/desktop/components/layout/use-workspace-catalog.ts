"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { WorkspaceTreeSource } from "@/components/task-tree/workspace-tree-model";
import { getWorkspaceTasks, getWorkspaces, type WorkspaceTask } from "@/lib/api/workspaces";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { readLastWorkspaceId, writeLastWorkspaceId } from "@/lib/workspace-preferences";

export type WorkspaceWithTasks = WorkspaceTreeSource;

/**
 * 侧栏工作区 / 任务目录与「当前任务、当前工作区」选择状态的唯一 owner。
 *
 * 刻意不用 TanStack Query：目录由全量加载与按工作区的定向刷新组合而成，二者用代号互相
 * 作废（全量加载期间到达的定向刷新排队、全量完成后补发），并且路由 task 可能先于侧栏
 * 列表被 GET /tasks/:id 确认。这些时序约束是侧栏特有的，留在这里集中维护。
 *
 * @param routeTaskId 当前路由上的 task（非任务路由为 null）。
 * @param workspaceConfigurationId 正在配置的工作区，打开工作区配置时同步为选中工作区。
 */
export function useWorkspaceCatalog(routeTaskId: number | null, workspaceConfigurationId: number | null) {
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<number | null>(null);
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<number[] | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceWithTasks[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  const loadGenerationRef = useRef(0);
  const loadControllerRef = useRef<AbortController | null>(null);
  const knownWorkspaceIdsRef = useRef(new Set<number>());
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
            taskLoadError: userErrorMessage(cause, "任务列表加载失败"),
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
      setError(userErrorMessage(cause, "工作区加载失败，请重试"));
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
    if (workspaceConfigurationId === null) return;
    setSelectedWorkspaceId(workspaceConfigurationId);
    writeLastWorkspaceId(workspaceConfigurationId);
  }, [workspaceConfigurationId]);

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
    void frontendLog("DEBUG", "workspace_route_changed", "AppLayout 观察到路由 task 变化", {
      data: { routeTaskId },
    });
  }, [routeTaskId]);

  const selectedWorkspace = useMemo(() => workspaces.find((workspace) => workspace.workspace_id === selectedWorkspaceId), [selectedWorkspaceId, workspaces]);
  const activeTask = useMemo(
    () => workspaces.flatMap((workspace) => workspace.tasks).find((task) => task.task_id === routeTaskId)
      ?? (routeTask?.task_id === routeTaskId ? routeTask : undefined),
    [routeTaskId, routeTask, workspaces],
  );
  const activeWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.workspace_id === (activeTask?.workspace_id ?? selectedWorkspaceId)),
    [activeTask?.workspace_id, selectedWorkspaceId, workspaces],
  );
  const activeTaskWorkspaceId = useMemo(() => {
    if (routeTaskId === null) return selectedWorkspaceId;
    if (routeTask?.task_id === routeTaskId) return routeTask.workspace_id;
    return workspaces.find((workspace) => workspace.tasks.some((task) => task.task_id === routeTaskId))?.workspace_id
      ?? null;
  }, [routeTaskId, routeTask, selectedWorkspaceId, workspaces]);

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
        ? { ...workspace, taskLoadError: userErrorMessage(cause, "任务列表加载失败") }
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
  const updateWorkspaceExpansion = useCallback((workspaceId: number, expanded: boolean) => {
    setExpandedWorkspaceIds((current) => {
      const ids = current ?? [];
      if (expanded) return ids.includes(workspaceId) ? ids : [...ids, workspaceId];
      return ids.filter((id) => id !== workspaceId);
    });
  }, []);
  const selectWorkspace = useCallback((workspaceId: number | null) => {
    setSelectedWorkspaceId(workspaceId);
    if (workspaceId !== null) writeLastWorkspaceId(workspaceId);
  }, []);

  return {
    workspaces,
    status,
    error,
    load,
    refreshWorkspaceTasks,
    selectedWorkspaceId,
    selectedWorkspace,
    selectWorkspace,
    expandedWorkspaceIds,
    updateWorkspaceExpansion,
    activeTask,
    activeWorkspace,
    activeTaskWorkspaceId,
    handleTaskLoaded,
  };
}

export type WorkspaceCatalog = ReturnType<typeof useWorkspaceCatalog>;
