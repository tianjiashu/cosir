import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { WorkspaceTreeSource } from "@/components/session-tree/workspace-tree-model";
import { getWorkspaceSessions, getWorkspaces, type WorkspaceSession } from "@/lib/api/workspaces";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useLastWorkspaceStore } from "@/lib/last-workspace-store";

/** 目录里的工作区：`sessions` 只含侧栏会话，未开始的会话单独给出（它就是这个工作区的新会话页）。 */
export type WorkspaceWithSessions = WorkspaceTreeSource & { unstartedSession: WorkspaceSession | null };

/** 后端的会话列表含未开始的会话；目录在这一处把它分出来，侧栏、命令面板等读到的都是分好的。 */
function splitUnstarted(all: WorkspaceSession[]): Pick<WorkspaceWithSessions, "sessions" | "unstartedSession"> {
  return {
    sessions: all.filter((session) => !session.unstarted),
    unstartedSession: all.find((session) => session.unstarted) ?? null,
  };
}

/** 目录里能按 id 找到的全部会话：侧栏会话加未开始的会话（后者不显示，但路由会打开它）。 */
function findCatalogSession(workspaces: readonly WorkspaceWithSessions[], sessionId: number): WorkspaceSession | undefined {
  for (const workspace of workspaces) {
    if (workspace.unstartedSession?.session_id === sessionId) return workspace.unstartedSession;
    const listed = workspace.sessions.find((session) => session.session_id === sessionId);
    if (listed) return listed;
  }
  return undefined;
}

/**
 * 侧栏工作区 / 会话目录与「当前会话、当前工作区」选择状态的唯一 owner。
 *
 * 何时重读只由后端决定：目录的写入方提交后经 Session 推送发出失效（`useSessionFeed`），工作区列表变了走
 * `load`、某个工作区的侧栏 Session 变了走 `refreshWorkspaceSessions`；此外只有挂载时的首次加载与推送重连
 * 后的整体重读。不按页面事件（标题变化、运行结束、卡片挂载）推断，也不在用户操作后自行重读——那样
 * 同一个变化有两条触发路径，后台会话 的变化还会漏掉。
 *
 * 刻意不用 TanStack Query：目录由全量加载与按工作区的定向刷新组合而成，二者用代号互相
 * 作废（全量加载期间到达的定向刷新排队、全量完成后补发），并且路由 session 可能先于侧栏
 * 列表被 GET /sessions/:id 确认。这些时序约束是侧栏特有的，留在这里集中维护。
 *
 * @param routeSessionId 当前路由上的 session（非会话路由为 null）。
 * @param workspaceConfigurationId 正在配置的工作区，打开工作区配置时同步为选中工作区。
 */
export function useWorkspaceCatalog(routeSessionId: number | null, workspaceConfigurationId: number | null) {
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<number | null>(null);
  const [expandedWorkspaceIds, setExpandedWorkspaceIds] = useState<number[] | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceWithSessions[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  const loadGenerationRef = useRef(0);
  const loadControllerRef = useRef<AbortController | null>(null);
  const knownWorkspaceIdsRef = useRef(new Set<number>());
  const catalogLoadingRef = useRef(false);
  const sessionRefreshGenerationRef = useRef(new Map<number, number>());
  const sessionRefreshControllerRef = useRef(new Map<number, AbortController>());
  const pendingSessionRefreshRef = useRef(new Set<number>());
  const [routeSession, setRouteSession] = useState<WorkspaceSession | null>(null);

  const load = useCallback(async () => {
    const generation = ++loadGenerationRef.current;
    catalogLoadingRef.current = true;
    void frontendLog("DEBUG", "workspace_catalog_load_started", "Workspace catalog 开始加载", {
      data: { generation, reason: "explicit_or_initial" },
    });
    loadControllerRef.current?.abort();
    for (const [workspaceId, controller] of sessionRefreshControllerRef.current.entries()) {
      pendingSessionRefreshRef.current.add(workspaceId);
      controller.abort();
    }
    const controller = new AbortController();
    loadControllerRef.current = controller;
    setStatus("loading");
    setError(null);
    try {
      const baseWorkspaces = await getWorkspaces({ signal: controller.signal });
      const withSessions = await Promise.all(baseWorkspaces.map(async (workspace) => {
        try {
          return { ...workspace, ...splitUnstarted(await getWorkspaceSessions(workspace.workspace_id, { signal: controller.signal })) };
        } catch (cause: unknown) {
          if (controller.signal.aborted) throw cause;
          return {
            ...workspace,
            sessions: [],
            unstartedSession: null,
            sessionLoadError: userErrorMessage(cause, "会话列表加载失败"),
          };
        }
      }));
      if (generation !== loadGenerationRef.current || controller.signal.aborted) {
        void frontendLog("DEBUG", "workspace_catalog_response_discarded", "Workspace catalog 丢弃过期响应", {
          data: { generation, currentGeneration: loadGenerationRef.current, aborted: controller.signal.aborted },
        });
        return;
      }
      setWorkspaces(withSessions);
      const receivedWorkspaceIds = new Set(withSessions.map((workspace) => workspace.workspace_id));
      setSelectedWorkspaceId((current) => {
        if (current && withSessions.some((workspace) => workspace.workspace_id === current)) return current;
        const lastWorkspaceId = useLastWorkspaceStore.getState().workspaceId;
        return lastWorkspaceId && withSessions.some((workspace) => workspace.workspace_id === lastWorkspaceId)
          ? lastWorkspaceId
          : null;
      });
      setExpandedWorkspaceIds((current) => {
        if (current === null) return withSessions.map((workspace) => workspace.workspace_id);
        const next = current.filter((id) => receivedWorkspaceIds.has(id));
        for (const workspace of withSessions) {
          if (!knownWorkspaceIdsRef.current.has(workspace.workspace_id)) next.push(workspace.workspace_id);
        }
        return next;
      });
      knownWorkspaceIdsRef.current = receivedWorkspaceIds;
      setStatus("ready");
      void frontendLog("DEBUG", "workspace_catalog_loaded", "Workspace catalog 加载完成", {
        data: { generation, workspaceCount: withSessions.length, sessionCount: withSessions.reduce((count, workspace) => count + workspace.sessions.length, 0) },
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
      for (const controller of sessionRefreshControllerRef.current.values()) controller.abort();
    };
  }, [load]);

  useEffect(() => {
    if (workspaceConfigurationId === null) return;
    setSelectedWorkspaceId(workspaceConfigurationId);
    useLastWorkspaceStore.getState().remember(workspaceConfigurationId);
  }, [workspaceConfigurationId]);

  // 打开一个会话时把它所在的工作区选中一次（列表里找到它或 GET /sessions/:id 确认它时），之后的目录刷新
  // 不再改选择：路由切换在 react-router 里是低优先级更新，「添加工作区后离开会话页」时目录刷新先于
  // 路由切换渲染，若每次刷新都重新选中，会把刚选中的新工作区拉回路由会话所在的工作区。
  const selectedForRouteSessionRef = useRef<number | null>(null);
  const selectRouteSessionWorkspace = useCallback((session: WorkspaceSession) => {
    if (selectedForRouteSessionRef.current === session.session_id) return;
    selectedForRouteSessionRef.current = session.session_id;
    setSelectedWorkspaceId(session.workspace_id);
    useLastWorkspaceStore.getState().remember(session.workspace_id);
  }, []);

  useEffect(() => {
    if (routeSessionId === null) {
      selectedForRouteSessionRef.current = null;
      setRouteSession(null);
      return;
    }
    const session = findCatalogSession(workspaces, routeSessionId);
    if (session) {
      setRouteSession(session);
      selectRouteSessionWorkspace(session);
      return;
    }
    // Keep a session already confirmed by GET /sessions/:id while a sidebar refresh
    // temporarily returns an incomplete list. A new route still drops the
    // previous session because its id no longer matches the URL.
    setRouteSession((current) => current?.session_id === routeSessionId ? current : null);
  }, [routeSessionId, selectRouteSessionWorkspace, workspaces]);

  useEffect(() => {
    void frontendLog("DEBUG", "workspace_route_changed", "AppLayout 观察到路由 session 变化", {
      data: { routeSessionId },
    });
  }, [routeSessionId]);

  const selectedWorkspace = useMemo(() => workspaces.find((workspace) => workspace.workspace_id === selectedWorkspaceId), [selectedWorkspaceId, workspaces]);
  const activeSession = useMemo(
    () => (routeSessionId === null ? undefined : findCatalogSession(workspaces, routeSessionId))
      ?? (routeSession?.session_id === routeSessionId ? routeSession : undefined),
    [routeSessionId, routeSession, workspaces],
  );
  const activeWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.workspace_id === (activeSession?.workspace_id ?? selectedWorkspaceId)),
    [activeSession?.workspace_id, selectedWorkspaceId, workspaces],
  );
  const activeSessionWorkspaceId = useMemo(() => {
    if (routeSessionId === null) return selectedWorkspaceId;
    if (routeSession?.session_id === routeSessionId) return routeSession.workspace_id;
    return findCatalogSession(workspaces, routeSessionId)?.workspace_id ?? null;
  }, [routeSessionId, routeSession, selectedWorkspaceId, workspaces]);

  const refreshWorkspaceSessions = useCallback(async (workspaceId: number) => {
    if (catalogLoadingRef.current) {
      pendingSessionRefreshRef.current.add(workspaceId);
      void frontendLog("DEBUG", "workspace_session_refresh_queued", "全量目录加载期间排队 workspace session 刷新", {
        data: { workspaceId, pendingCount: pendingSessionRefreshRef.current.size },
      });
      return;
    }
    const nextGeneration = (sessionRefreshGenerationRef.current.get(workspaceId) ?? 0) + 1;
    sessionRefreshGenerationRef.current.set(workspaceId, nextGeneration);
    sessionRefreshControllerRef.current.get(workspaceId)?.abort();
    const controller = new AbortController();
    sessionRefreshControllerRef.current.set(workspaceId, controller);
    const catalogGeneration = loadGenerationRef.current;
    void frontendLog("DEBUG", "workspace_session_refresh_started", "Workspace session 列表开始定向刷新", {
      data: { workspaceId, requestGeneration: nextGeneration, catalogGeneration },
    });
    try {
      const { sessions, unstartedSession } = splitUnstarted(await getWorkspaceSessions(workspaceId, { signal: controller.signal }));
      if (
        controller.signal.aborted
        || catalogGeneration !== loadGenerationRef.current
        || nextGeneration !== sessionRefreshGenerationRef.current.get(workspaceId)
      ) return;
      setWorkspaces((current) => current.map((workspace) => workspace.workspace_id === workspaceId
        ? { ...workspace, sessions, unstartedSession, sessionLoadError: undefined }
        : workspace));
      void frontendLog("DEBUG", "workspace_session_refresh_completed", "Workspace session 列表定向刷新完成", {
        data: { workspaceId, requestGeneration: nextGeneration, sessionCount: sessions.length },
      });
    } catch (cause: unknown) {
      if (controller.signal.aborted) return;
      if (catalogGeneration !== loadGenerationRef.current || nextGeneration !== sessionRefreshGenerationRef.current.get(workspaceId)) return;
      setWorkspaces((current) => current.map((workspace) => workspace.workspace_id === workspaceId
        ? { ...workspace, sessionLoadError: userErrorMessage(cause, "会话列表加载失败") }
        : workspace));
      void frontendLog("WARNING", "workspace_session_refresh_failed", "Workspace session 列表定向刷新失败", {
        data: { workspaceId, requestGeneration: nextGeneration },
        error: cause,
      });
    } finally {
      if (sessionRefreshControllerRef.current.get(workspaceId) === controller) {
        sessionRefreshControllerRef.current.delete(workspaceId);
      }
    }
  }, []);
  useEffect(() => {
    if (status === "loading" || pendingSessionRefreshRef.current.size === 0) return;
    const pendingWorkspaceIds = [...pendingSessionRefreshRef.current];
    pendingSessionRefreshRef.current.clear();
    for (const workspaceId of pendingWorkspaceIds) void refreshWorkspaceSessions(workspaceId);
  }, [refreshWorkspaceSessions, status]);

  const handleSessionLoaded = useCallback((session: WorkspaceSession) => {
    if (session.session_id !== routeSessionId) return;
    setRouteSession(session);
    selectRouteSessionWorkspace(session);
  }, [routeSessionId, selectRouteSessionWorkspace]);
  const updateWorkspaceExpansion = useCallback((workspaceId: number, expanded: boolean) => {
    setExpandedWorkspaceIds((current) => {
      const ids = current ?? [];
      if (expanded) return ids.includes(workspaceId) ? ids : [...ids, workspaceId];
      return ids.filter((id) => id !== workspaceId);
    });
  }, []);
  const selectWorkspace = useCallback((workspaceId: number | null) => {
    setSelectedWorkspaceId(workspaceId);
    if (workspaceId !== null) useLastWorkspaceStore.getState().remember(workspaceId);
  }, []);

  return {
    workspaces,
    status,
    error,
    load,
    refreshWorkspaceSessions,
    selectedWorkspaceId,
    selectedWorkspace,
    selectWorkspace,
    expandedWorkspaceIds,
    updateWorkspaceExpansion,
    activeSession,
    activeWorkspace,
    activeSessionWorkspaceId,
    handleSessionLoaded,
  };
}

export type WorkspaceCatalog = ReturnType<typeof useWorkspaceCatalog>;
