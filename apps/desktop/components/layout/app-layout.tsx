import { ReferenceBridgeProvider, ReferenceTargetScope } from "@/components/workspace-references/reference-bridge";

import { WorkspaceFileWatches } from "@/components/workspace-files/workspace-file-watches";
import { useCallback, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import {
  Outlet,
  useLocation,
  useMatches,
  useNavigate,
  useOutletContext,
} from "react-router-dom";
import { FolderIcon, GlobeIcon, LayoutListIcon, PanelRightIcon, SquareTerminalIcon } from "lucide-react";
import { useHotkeys } from "react-hotkeys-hook";

import { ConversationAsideContext, type ConversationAside } from "@/components/conversation/thread/conversation-aside";
import { EmptyThreadFrameContext, type EmptyThreadFrame } from "@/components/conversation/thread/empty-thread-frame";
import { NewConversationFrame } from "@/components/new-conversation/new-conversation-frame";
import type { NewConversationRouteState } from "@/components/new-conversation/new-conversation";
import { ConversationAsidePanel } from "@/components/conversation-aside/conversation-aside-panel";
import { useConversationAside } from "@/components/conversation-aside/use-conversation-aside";
import { CONVERSATION_ASIDE_WIDTH } from "@/lib/conversation-aside/placement";
import { CommandPalette } from "@/components/command-palette/command-palette";
import { BackendStatusBanner } from "@/components/backend/status-banner";
import { confirmBackendRestart } from "@/components/backend/backend-restart";
import { canRestartBackendRuntime } from "@/lib/backend/runtime-config";
import { AppSidebar, type SidebarSection } from "@/components/layout/sidebar/app-sidebar";
import { SidebarControls } from "@/components/layout/sidebar/sidebar-controls";
import { PageHeader, type Crumb } from "@/components/layout/page-header";
import { useWorkspaceActions } from "@/components/layout/use-workspace-actions";
import { useWorkspaceCatalog, type WorkspaceCatalog } from "@/components/layout/use-workspace-catalog";
import { IsolatedRegion } from "@/components/render-isolation/isolated-region";
import { configurationPath, resolveConfigurationTab, SYSTEM_CONFIGURATION_TABS, WORKSPACE_CONFIGURATION_TABS } from "@/components/configuration/configuration-tabs";
import { SessionPage } from "@/components/session/session-page";
import { StatusDot } from "@/components/ui/status-dot";
import { StatusLine } from "@/components/ui/status-line";
import { PanelResizeHandle } from "@/components/ui/panel-resize-handle";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { Workbench } from "@/components/workbench/workbench";
import { BrowserPanel } from "@/components/browser/browser-panel";
import { FilesPanel } from "@/components/workspace-files/workspace-files-panel";
import { desktopHost } from "@/lib/desktop-host";
import { useDockPanels } from "./use-dock-panels";
import { useUnsavedRouteGuard } from "./use-unsaved-route-guard";
import { useMediaQuery } from "@/hooks/use-media-query";
import { useAppHotkey, withShortcut } from "@/lib/keyboard/shortcuts";
import {
  selectTerminalSession,
  useTerminalBadge,
  useTerminalWorkbenchStore,
} from "@/lib/terminal/terminal-workbench-store";
import { WorkbenchProvider } from "@/lib/workbench/workbench-context";
import { SIDEBAR_WIDTH, useLayoutPreferences } from "@/lib/layout/preferences";
import { cn } from "@/lib/utils";
import { useSessionFeed } from "@/lib/session-feed/use-session-feed";
import { useMarkSessionRead } from "@/lib/session-status/use-mark-session-read";
import { sessionDisplayTitle } from "@/lib/session-title/session-display-title";

/** 路由表上每个子路由通过 handle 声明自己属于哪个区域（见 src/router.tsx）。 */
export type RouteHandle = { section: SidebarSection; files?: boolean };

export type AppOutletContext = {
  catalog: WorkspaceCatalog;
  actions: ReturnType<typeof useWorkspaceActions>;
};

/** 子路由读取布局持有的目录与动作。 */
export function useAppOutlet(): AppOutletContext {
  return useOutletContext<AppOutletContext>();
}

const NARROW_VIEWPORT_QUERY = "(max-width: 1024px)";

/** 路由里的会话、工作区标识：正整数，否则为 null（由各路由给出「标识无效」）。 */
export function parseRouteId(value: string | undefined): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

/**
 * 应用的唯一页面骨架（布局路由）：侧栏 + 页面头部 + 主区域 + 工作台。
 *
 * 关键约束：
 * - 会话保活。离开对话去看配置或会话观测时，最近的会话页保持挂载（被内容层覆盖并设为
 *   inert），回来时 Assistant 会话、草稿与运行中的订阅都还在；只有进入另一个会话或新会话才替换。
 *   会话页始终由这里渲染（不经 Outlet），因此路由切换不会重建它。
 * - 未保存修改。离开配置路由（含切换配置标签）前统一经 useBlocker 询问，页面内不再各自确认。
 * - 新会话页就是所选工作区未开始的会话：`/` 只负责解析并转到它（`NewConversation`），之后由会话页渲染，
 *   面包屑是「新会话」，空态经 `EmptyThreadFrameContext` 用新会话页的版面。判定只看会话自己的
 *   `unstarted`；首个 Run 建立后目录推送把它挪进侧栏，地址不变、会话页不重挂。
 * - 侧栏：宽屏停靠，收起即整个隐藏（偏好持久化）；窄屏（≤1024px）平时隐藏，打开时以抽屉覆盖主区域。
 *   开关（`SidebarControls`）钉在红绿灯右侧，不随侧栏状态移动：总是渲染在此刻左上角那一行的拖动区里。
 */
export function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const matches = useMatches();
  const leaf = matches.at(-1);
  const section = (leaf?.handle as RouteHandle | undefined)?.section ?? "conversation";
  const params = leaf?.params ?? {};

  const routeSessionId = section === "conversation" ? parseRouteId(params.sessionId) : null;
  const workspaceConfigurationId = section === "workspace-configuration" ? parseRouteId(params.workspaceId) : null;

  // 最近一个会话：对话区为当前路由会话；其余区域沿用上一次的会话（保活）。
  const [keptSessionId, setKeptSessionId] = useState<number | null>(routeSessionId);
  useEffect(() => {
    if (section !== "conversation") return;
    setKeptSessionId(routeSessionId);
  }, [routeSessionId, section]);
  const filesVisible = Boolean((leaf?.handle as RouteHandle | undefined)?.files) && (!params.sessionId || routeSessionId !== null);
  const focusedSessionId = section === "conversation" ? routeSessionId : keptSessionId;

  const catalog = useWorkspaceCatalog(focusedSessionId, workspaceConfigurationId);
  // 刚从 `/` 取或建过来时目录推送可能还没到：用随导航带来的取或建结果，不再请求一次。
  const arrived = (location.state as NewConversationRouteState | null)?.session;
  const focusedSession = catalog.activeSession ?? (arrived?.session_id === focusedSessionId ? arrived : undefined);
  useSessionFeed({ onWorkspacesChanged: catalog.load, onWorkspaceSessionsChanged: catalog.refreshWorkspaceSessions });
  useUnsavedRouteGuard();
  // 工作台显示聚焦会话所在工作区的桶（无会话时为选中工作区）；归属尚在加载时暂不显示，不销毁任何内容。
  const workbenchWorkspace = catalog.workspaces.find((workspace) => workspace.workspace_id === catalog.activeSessionWorkspaceId);
  const workbenchWorkspaceId = workbenchWorkspace?.workspace_id ?? null;
  const actions = useWorkspaceActions(catalog, focusedSessionId);

  // ---- 侧栏：宽屏停靠（可收起并记住），窄屏抽屉 ----
  const narrow = useMediaQuery(NARROW_VIEWPORT_QUERY);
  const dockedCollapsed = useLayoutPreferences((state) => state.sidebarCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const sidebarDocked = !narrow && !dockedCollapsed;
  const sidebarOpen = narrow ? drawerOpen : sidebarDocked;
  // 展开宽度是用户偏好：拖拽右缘调整，宽屏停靠与窄屏抽屉共用。
  const sidebarWidth = useLayoutPreferences((state) => state.sidebarWidth);
  const resizeSidebar = useLayoutPreferences((state) => state.setSidebarWidth);
  const toggleSidebar = useCallback(() => {
    if (narrow) {
      setDrawerOpen((open) => !open);
      return;
    }
    const { sidebarCollapsed, setSidebarCollapsed } = useLayoutPreferences.getState();
    setSidebarCollapsed(!sidebarCollapsed);
  }, [narrow]);
  useEffect(() => {
    if (!narrow) setDrawerOpen(false);
  }, [narrow]);
  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);
  useHotkeys("escape", () => setDrawerOpen(false), { enabled: drawerOpen, enableOnFormTags: true });

  // ---- 应用级快捷键（登记见 lib/keyboard/shortcuts.ts）；新会话在导航意图处、工作台与会话侧栏由各自的 hook 注册 ----
  useAppHotkey("toggleSidebar", toggleSidebar);
  useAppHotkey("openConfiguration", () => navigate(configurationPath(null)));

  // ---- 终端工作台：入口在页头，面板在 SessionPage 内，二者经按 sessionId 分桶的 store 协作 ----
  // 对话页的 Session：终端与会话侧栏都只属于对话区，配置 / 观测页不显示。
  const conversationSessionId = section === "conversation" ? routeSessionId : null;
  // 右侧停靠区：文件、内置浏览器、工作台三选一，各自独立（见 useDockPanels / lib/layout/dock-store）。
  const dock = useDockPanels({ workspaceId: workbenchWorkspaceId, filesVisible, conversationSessionId });
  const terminalPanelOpen = useTerminalWorkbenchStore((state) => conversationSessionId === null ? false : selectTerminalSession(conversationSessionId)(state).open);
  const terminalBadge = useTerminalBadge(conversationSessionId);
  const toggleTerminal = useCallback(() => {
    if (conversationSessionId !== null) useTerminalWorkbenchStore.getState().toggle(conversationSessionId);
  }, [conversationSessionId]);
  useAppHotkey("toggleTerminal", toggleTerminal, { enabled: conversationSessionId !== null });

  // ---- 会话侧栏：开关、摆放、页头角标与快捷键见 useConversationAside ----
  const rail = useConversationAside(conversationSessionId);
  // 对话页始终给视口一个附注：显隐要看视口量出的摆放，由 ConversationAsidePanel 自己决定；崩溃兜底占网格右列，与固定时的位置一致。
  const conversationAside = useMemo<ConversationAside | null>(
    () => conversationSessionId === null ? null : (viewport) => (
      <IsolatedRegion
        region="conversation-aside"
        label="会话侧栏"
        resetKey={`conversation-aside:${conversationSessionId}`}
        fallbackClassName="dock-panel bg-background col-start-2 row-start-1 m-2 w-(--conversation-aside-width) self-start p-3"
        escape={{ label: "收起会话侧栏", onClick: (reset) => { rail.close(); reset(); } }}
      >
        <ConversationAsidePanel sessionId={conversationSessionId} viewport={viewport} onClose={rail.close} />
      </IsolatedRegion>
    ),
    [conversationSessionId, rail.close],
  );

  // ---- 命令面板（`commandPalette`）：搜索会话、开始新会话、跳转配置 ----
  const [paletteOpen, setPaletteOpen] = useState(false);
  useAppHotkey("commandPalette", () => setPaletteOpen(true));

  // ---- 导航意图 ----
  // 开始新会话的唯一入口（侧栏「新会话」与工作区「+」、快捷键 `newConversation`、命令面板）：带工作区时先显式选中它，
  // 不带时沿用当前选择。选中工作区只经这类显式意图改变，点侧栏工作区行不会改。
  const { selectWorkspace } = catalog;
  const startNewConversation = useCallback((workspaceId?: number) => {
    if (workspaceId !== undefined) selectWorkspace(workspaceId);
    navigate("/");
  }, [navigate, selectWorkspace]);
  useAppHotkey("newConversation", () => startNewConversation());
  const selectSession = useCallback((session: { session_id: number }) => {
    navigate(`/sessions/${session.session_id}`);
  }, [navigate]);
  const conversationPath = keptSessionId === null ? "/" : `/sessions/${keptSessionId}`;

  // ---- 未开始的会话就是它所在工作区的新会话页：面包屑与空态版面都按新会话页来 ----
  const focusedUnstarted = focusedSession?.unstarted ?? false;
  const { workspaces: catalogWorkspaces } = catalog;
  const focusedWorkspaceId = focusedSession?.workspace_id ?? null;
  const { addWorkspaceFromFolder, addingWorkspace } = actions;
  const newConversationFrame = useMemo<EmptyThreadFrame | null>(
    () => !focusedUnstarted ? null : (composer) => (
      <NewConversationFrame
        workspaces={catalogWorkspaces}
        selectedWorkspaceId={focusedWorkspaceId}
        // 换工作区即去那个工作区的新会话页；这个会话与它的终端、标签留在原工作区，回来还在。
        onWorkspaceChange={startNewConversation}
        onAddWorkspace={addWorkspaceFromFolder}
        addingWorkspace={addingWorkspace}
      >
        {composer}
      </NewConversationFrame>
    ),
    [addWorkspaceFromFolder, addingWorkspace, catalogWorkspaces, focusedUnstarted, focusedWorkspaceId, startNewConversation],
  );

  const crumbs = useMemo<Crumb[]>(() => {
    // Skill 详情页在标签之后多一级：标签那一级变成可返回的链接。
    const tabCrumbs = (tab: { id: string; label: string } | undefined, basePath: string): Crumb[] =>
      params.skillName === undefined
        ? [{ label: tab?.label ?? "未知分类" }]
        : [{ label: tab?.label ?? "未知分类", to: `${basePath}/${tab?.id}` }, { label: params.skillName }];
    if (section === "configuration") {
      const tab = resolveConfigurationTab(SYSTEM_CONFIGURATION_TABS, { tab: params.tab, skillName: params.skillName });
      return [{ label: "系统配置", to: configurationPath(null) }, ...tabCrumbs(tab, configurationPath(null))];
    }
    if (section === "workspace-configuration") {
      const workspace = catalog.workspaces.find((candidate) => candidate.workspace_id === workspaceConfigurationId);
      const tab = resolveConfigurationTab(WORKSPACE_CONFIGURATION_TABS, { tab: params.tab, skillName: params.skillName });
      const basePath = configurationPath(workspaceConfigurationId);
      return [
        { label: workspace?.name ?? "工作区", to: conversationPath },
        { label: "工作区配置", to: basePath },
        ...tabCrumbs(tab, basePath),
      ];
    }
    if (section === "observer") return [{ label: "会话观测" }];
    if (routeSessionId === null || focusedUnstarted) return [{ label: "新会话" }];
    return [
      { label: catalog.activeWorkspace?.name ?? "工作区" },
      { label: sessionDisplayTitle(catalog.activeSession?.title ?? "") },
    ];
  }, [catalog.activeSession?.title, catalog.activeWorkspace?.name, catalog.workspaces, conversationPath, focusedUnstarted, params.skillName, params.tab, routeSessionId, section, workspaceConfigurationId]);

  const showSessionLayer = section === "conversation" && routeSessionId !== null;
  // 正显示的会话页有未读结果时告诉后端看过了（切到配置页时会话页保活，但你不在看它）。
  useMarkSessionRead(showSessionLayer ? routeSessionId : null);
  const outletContext = useMemo<AppOutletContext>(() => ({ catalog, actions }), [actions, catalog]);

  // 侧栏开关只有一份状态，同一时刻只渲染在此刻左上角那一行：侧栏可见（停靠或抽屉）时在侧栏头部，否则在页面头部。
  const sidebarControls = <SidebarControls open={sidebarOpen} onToggle={toggleSidebar} onNewConversation={() => startNewConversation()} />;
  const sidebar = (headerControls: ReactNode) => (
    <AppSidebar
      headerControls={headerControls}
      catalog={catalog}
      // 未开始的会话不在侧栏里：新会话页在侧栏上表现为它所在的工作区被选中。
      focusedSessionId={focusedUnstarted ? null : focusedSessionId}
      section={section}
      newConversationActive={section === "conversation" && (location.pathname === "/" || focusedUnstarted)}
      onNewConversation={startNewConversation}
      onSelectSession={selectSession}
      onOpenConfiguration={() => navigate(configurationPath(null))}
      onOpenWorkspaceConfiguration={(workspaceId) => navigate(configurationPath(workspaceId))}
      onOpenObserver={() => navigate(focusedSessionId ? `/session-observer?session_id=${focusedSessionId}` : "/session-observer", { state: { returnTo: focusedSessionId ? `/sessions/${focusedSessionId}` : undefined } })}
      onOpenSearch={() => setPaletteOpen(true)}
      onRequestDelete={(target) => void actions.requestDelete(target)}
    />
  );

  // 引用跳转与工作台共用同一个工作区来源：文件标签总是打开到当前显示的那个工作台桶里。
  return (
    <ReferenceBridgeProvider workspaceRoot={workbenchWorkspace?.root_path ?? ""} workspaceName={workbenchWorkspace?.name ?? "工作区"} target={filesVisible && routeSessionId ? `session:${routeSessionId}` : null} workspaceId={workbenchWorkspaceId}>
    <WorkbenchProvider workspaceId={workbenchWorkspaceId}>
      {/* 根是下沉的窗口底色，侧栏直接铺在上面；主区与停靠卡片在 bg-background 上，二者靠明度差分隔，不画线。 */}
      <div className="bg-canvas relative flex h-dvh min-h-0 overflow-hidden">
        <BackendStatusBanner />
        <WorkspaceFileWatches />
        {/* 收起时宽度过渡到 0 而不是卸载：有收起动画，侧栏滚动位置与展开状态也保留；inert 让它不可聚焦。 */}
        {!narrow && (
          <aside
            style={{ width: sidebarDocked ? sidebarWidth : 0 }}
            inert={!sidebarDocked}
            className={cn(
              "relative flex min-h-0 shrink-0 flex-col",
              // 拖拽调宽时关掉过渡，否则宽度追着指针慢半拍。
              "transition-[width] duration-200 motion-reduce:transition-none [&:has(>[role=separator]:active)]:transition-none",
            )}
          >
            {sidebar(sidebarDocked ? sidebarControls : null)}
            {sidebarDocked && (
              <PanelResizeHandle
                className="absolute inset-y-0 -right-1 bg-transparent [&>svg]:opacity-0 hover:[&>svg]:opacity-100 focus-visible:[&>svg]:opacity-100"
                label="调整侧栏宽度"
                value={sidebarWidth}
                min={SIDEBAR_WIDTH.min}
                max={SIDEBAR_WIDTH.max}
                onChange={resizeSidebar}
              />
            )}
          </aside>
        )}
        {narrow && drawerOpen && (
          <>
            <div className="absolute inset-0 z-40 bg-black/20" aria-hidden="true" onClick={() => setDrawerOpen(false)} />
            <aside style={{ width: sidebarWidth }} className="bg-canvas absolute inset-y-0 left-0 z-50 flex min-h-0 flex-col shadow-xl" aria-label="侧栏">
              {sidebar(sidebarControls)}
            </aside>
          </>
        )}

        {/*
          页面头部让位：右上角有工作台卡片时由卡片标签行让位，否则头部让位（以 DOM 中是否
          渲染了工作台为准，工作台自身还有多种不渲染的情形）；侧栏不停靠时头部就是左上角那一行，
          左侧让出红绿灯（与侧栏头部同一内边距，侧栏开关因此不移位）。
        */}
        <div
          ref={dock.containerRef}
          className={cn(
            "bg-background flex min-h-0 min-w-0 flex-1 [--header-inset-right:var(--titlebar-inset-right)] has-[[data-dock-panel]]:[--header-inset-right:0px]",
            !sidebarDocked && "[--header-inset-left:var(--titlebar-inset-left)]",
          )}
        >
        {/* 浏览器铺满时对话区让出整行；用 hidden 而不是卸载：会话页与 Assistant runtime 保持挂载。 */}
        <main className={cn("flex min-h-0 min-w-0 flex-1 flex-col", dock.maximized && "hidden")}>
          <PageHeader
            leading={sidebarOpen ? undefined : sidebarControls}
            crumbs={crumbs}
            actions={(
              <>
                {conversationSessionId !== null && (
                  <TooltipIconButton
                    tooltip={withShortcut(
                      [terminalPanelOpen ? "收起终端" : "打开终端", terminalBadge.count > 0 && `${terminalBadge.count} 个会话`, terminalBadge.hint].filter(Boolean).join(" · "),
                      "toggleTerminal",
                    )}
                    aria-label={`终端，${terminalBadge.count} 个运行中会话${terminalBadge.hint ? `，${terminalBadge.hint}` : ""}，${terminalPanelOpen ? "点击收起" : "点击打开"}`}
                    aria-pressed={terminalPanelOpen}
                    className={cn("relative size-7", terminalPanelOpen && "bg-muted text-foreground")}
                    onClick={toggleTerminal}
                  >
                    <SquareTerminalIcon />
                    {terminalBadge.tone !== null && <StatusDot tone={terminalBadge.tone} />}
                  </TooltipIconButton>
                )}
                {conversationSessionId !== null && desktopHost !== null && (
                  <TooltipIconButton
                    tooltip={[dock.panel === "browser" ? "收起浏览器" : "打开浏览器", dock.browserBadge.count > 0 && `${dock.browserBadge.count} 个标签页`, dock.browserBadge.hint].filter(Boolean).join(" · ")}
                    aria-label={`浏览器，${dock.browserBadge.count} 个标签页${dock.browserBadge.hint ? `，${dock.browserBadge.hint}` : ""}，${dock.panel === "browser" ? "点击收起" : "点击打开"}`}
                    aria-pressed={dock.panel === "browser"}
                    className={cn("relative size-7", dock.panel === "browser" && "bg-muted text-foreground")}
                    onClick={() => dock.toggle("browser")}
                  >
                    <GlobeIcon />
                    {dock.browserBadge.tone !== null && <StatusLine tone={dock.browserBadge.tone === "browsing" ? "breathing" : "static"} />}
                  </TooltipIconButton>
                )}
                {dock.available.files && (
                  <TooltipIconButton
                    tooltip="浏览工作区文件"
                    aria-label="浏览工作区文件"
                    aria-pressed={dock.panel === "files"}
                    className={cn("size-7", dock.panel === "files" && "bg-muted text-foreground")}
                    onClick={() => dock.toggle("files")}
                  >
                    <FolderIcon />
                  </TooltipIconButton>
                )}
                {conversationSessionId !== null && rail.hasContent && (
                  <TooltipIconButton
                    ref={rail.buttonRef}
                    tooltip={withShortcut([rail.visible ? "收起会话侧栏" : "打开会话侧栏", rail.runningCount > 0 && `${rail.runningCount} 个后台任务运行中`].filter(Boolean).join(" · "), "toggleConversationAside")}
                    aria-label={`会话侧栏，${rail.runningCount} 个后台任务运行中`}
                    aria-pressed={rail.visible}
                    className={cn("relative size-7", rail.visible && "bg-muted text-foreground")}
                    onClick={rail.toggle}
                  >
                    <LayoutListIcon />
                    {rail.runningCount > 0 && <StatusDot tone="activity" />}
                  </TooltipIconButton>
                )}
                {dock.available.workbench && (
                  <TooltipIconButton
                    tooltip={withShortcut(`${dock.panel === "workbench" ? "收起工作台" : "打开工作台"} · ${dock.workbenchTabs.length} 个标签`, "toggleWorkbench")}
                    aria-label={`工作台，${dock.workbenchTabs.length} 个标签，${dock.panel === "workbench" ? "点击收起" : "点击打开"}`}
                    aria-pressed={dock.panel === "workbench"}
                    className={cn("size-7", dock.panel === "workbench" && "bg-muted text-foreground")}
                    onClick={() => dock.toggle("workbench")}
                  >
                    <PanelRightIcon />
                  </TooltipIconButton>
                )}
              </>
            )}
          />
          {/*
            会话侧栏不在这里占列：它是对话画布上的附注，经 ConversationAsideContext 交给对话视口，
            与内容列同在滚动容器的网格里（见 conversation-aside.tsx）。崩溃兜底只能给 class，经
            `--conversation-aside-width` 与侧栏取同一个宽度常量。
          */}
          <div style={{ "--conversation-aside-width": `${CONVERSATION_ASIDE_WIDTH}px` } as CSSProperties} className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
            {focusedSessionId !== null && (
              <div className="h-full" inert={!showSessionLayer} aria-hidden={!showSessionLayer || undefined}>
                <ConversationAsideContext.Provider value={conversationAside}>
                <EmptyThreadFrameContext.Provider value={newConversationFrame}>
                <ReferenceTargetScope.Provider value={`session:${focusedSessionId}`}><SessionPage
                  key={focusedSessionId}
                  sessionId={focusedSessionId}
                  initialSession={focusedSession}
                  workspaceRoot={catalog.activeWorkspace?.root_path}
                  forkingMessageId={actions.forkingMessageId}
                  onForkMessage={actions.forkMessage}
                  onSessionLoaded={catalog.handleSessionLoaded}
                /></ReferenceTargetScope.Provider>
                </EmptyThreadFrameContext.Provider>
                </ConversationAsideContext.Provider>
              </div>
            )}
            {!showSessionLayer && (
              <div className={cn("bg-background h-full", focusedSessionId !== null && "absolute inset-0 z-10")}>
                <IsolatedRegion
                  region={section}
                  label={crumbs.at(-1)?.label ?? "页面"}
                  resetKey={location.pathname}
                  fallbackClassName="flex h-full items-center justify-center p-6"
                  escape={section === "conversation" ? undefined : { label: "返回会话", onClick: () => navigate(conversationPath) }}
                >
                  <Outlet context={outletContext} />
                </IsolatedRegion>
              </div>
            )}
          </div>
        </main>

        <IsolatedRegion
          region="dock"
          label="右侧面板"
          // 换面板或收起会改变 resetKey，从而清除崩溃状态；下次打开重新挂载。
          resetKey={`dock:${dock.panel ?? "none"}:${workbenchWorkspaceId ?? "none"}`}
          fallbackClassName="dock-panel bg-background m-2 w-80 shrink-0 p-3"
          escape={{ label: "收起面板", onClick: dock.close }}
        >
          {dock.sizing && dock.panel === "workbench" && workbenchWorkspace && (
            <Workbench workspace={workbenchWorkspace} tabs={dock.workbenchTabs} activeTabId={dock.workbenchActiveTabId}
              sizing={dock.sizing} onRequestClose={dock.close} />
          )}
          {dock.sizing && dock.panel === "files" && workbenchWorkspace && (
            <FilesPanel workspace={workbenchWorkspace} sizing={dock.sizing}
              maximized={dock.maximized} onToggleMaximized={dock.toggleMaximized} onClose={dock.close} />
          )}
          {dock.sizing && dock.panel === "browser" && (
            <BrowserPanel sessionId={dock.browserSessionId} sizing={dock.sizing}
              maximized={dock.maximized} onToggleMaximized={dock.toggleMaximized} onClose={dock.close} />
          )}
        </IsolatedRegion>
        </div>
      </div>
      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        workspaces={catalog.workspaces}
        onSelectSession={selectSession}
        onNewConversation={() => startNewConversation()}
        onNavigate={navigate}
        onRestartBackend={canRestartBackendRuntime() ? () => confirmBackendRestart("command_palette") : undefined}
      />
    </WorkbenchProvider></ReferenceBridgeProvider>
  );
}
