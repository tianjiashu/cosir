import { ReferenceBridgeProvider, ReferenceTargetScope } from "@/components/workspace-references/reference-bridge";

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import {
  Outlet,
  useLocation,
  useMatches,
  useNavigate,
  useOutletContext,
} from "react-router-dom";
import { FolderIcon, GlobeIcon, LayoutListIcon, PanelRightIcon, SquareTerminalIcon } from "lucide-react";
import { useHotkeys } from "react-hotkeys-hook";

import { ConversationAsideContext, type ConversationAside } from "@/components/assistant-ui/elements/conversation-aside";
import { TaskRail } from "@/components/task-rail/task-rail";
import { useTaskRail } from "@/components/task-rail/use-task-rail";
import { TASK_RAIL_WIDTH } from "@/lib/task-rail/placement";
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
import { findSettingsTab, SYSTEM_SETTINGS_TABS, WORKSPACE_SETTINGS_TABS } from "@/components/settings/settings-tabs";
import { TaskPage } from "@/components/task-page";
import { TaskRunStatusBadge } from "@/components/task-tree/task-run-status";
import { StatusDot } from "@/components/ui/status-dot";
import { PanelResizeHandle } from "@/components/ui/panel-resize-handle";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { Workbench } from "@/components/workbench/workbench";
import { BrowserPanel } from "@/components/browser/browser-panel";
import { FilesPanel } from "@/components/files/files-panel";
import { desktopHost } from "@/lib/desktop-host";
import { useDockPanels } from "./use-dock-panels";
import { useUnsavedRouteGuard } from "./use-unsaved-route-guard";
import { useMediaQuery } from "@/hooks/use-media-query";
import { APP_HOTKEY_OPTIONS, APP_SHORTCUTS, withShortcut } from "@/lib/keyboard/shortcuts";
import {
  selectTerminalTask,
  useTerminalBadge,
  useTerminalWorkbenchStore,
} from "@/lib/terminal/terminal-workbench-store";
import { WorkbenchProvider } from "@/lib/workbench/context";
import { SIDEBAR_WIDTH, useLayoutPreferences } from "@/lib/layout/preferences";
import { cn } from "@/lib/utils";
import { useTaskExecutionStatus } from "@/lib/task-status/task-status-store";
import { useTaskStatusStream } from "@/lib/task-status/use-task-status-stream";

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

function parsePositiveId(value: string | undefined): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

/**
 * 应用的唯一页面骨架（布局路由）：侧栏 + 页面头部 + 主区域 + 工作台。
 *
 * 关键约束：
 * - 任务会话保活。离开对话去看配置或上下文观测时，最近的任务页保持挂载（被内容层覆盖并设为
 *   inert），回来时 Assistant 会话、草稿与运行中的订阅都还在；只有进入另一个任务或新对话才替换。
 *   任务页始终由这里渲染（不经 Outlet），因此路由切换不会重建它。
 * - 未保存修改。离开配置路由（含切换配置标签）前统一经 useBlocker 询问，页面内不再各自确认。
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

  const routeTaskId = section === "conversation" ? parsePositiveId(params.taskId) : null;
  const workspaceConfigurationId = section === "workspace-settings" ? parsePositiveId(params.workspaceId) : null;

  // 最近一个任务：对话区为当前路由任务；其余区域沿用上一次的任务（保活）。
  const [keptTaskId, setKeptTaskId] = useState<number | null>(routeTaskId);
  useEffect(() => {
    if (section !== "conversation") return;
    setKeptTaskId(routeTaskId);
  }, [routeTaskId, section]);
  const filesVisible = Boolean((leaf?.handle as RouteHandle | undefined)?.files) && (!params.taskId || routeTaskId !== null);
  const focusedTaskId = section === "conversation" ? routeTaskId : keptTaskId;

  const catalog = useWorkspaceCatalog(focusedTaskId, workspaceConfigurationId);
  useTaskStatusStream();
  const activeTaskStatus = useTaskExecutionStatus(catalog.activeTask?.task_id);
  useUnsavedRouteGuard();
  // 工作台显示聚焦任务所在工作区的桶（无任务时为选中工作区）；归属尚在加载时暂不显示，不销毁任何内容。
  const workbenchWorkspace = catalog.workspaces.find((workspace) => workspace.workspace_id === catalog.activeTaskWorkspaceId);
  const workbenchWorkspaceId = workbenchWorkspace?.workspace_id ?? null;
  // 快捷键先于导航函数注册，经 ref 调用最新的实现。
  const startNewConversationRef = useRef<(workspaceId?: number) => void>(() => undefined);
  const actions = useWorkspaceActions(catalog, focusedTaskId);

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

  // ---- 应用级快捷键（登记见 lib/keyboard/shortcuts.ts）；工作台与会话侧栏的快捷键由各自的 hook 注册 ----
  useHotkeys(APP_SHORTCUTS.newConversation.keys, () => startNewConversationRef.current(), APP_HOTKEY_OPTIONS);
  useHotkeys(APP_SHORTCUTS.toggleSidebar.keys, () => toggleSidebar(), APP_HOTKEY_OPTIONS, [toggleSidebar]);
  useHotkeys(APP_SHORTCUTS.openSettings.keys, () => navigate("/settings"), APP_HOTKEY_OPTIONS, [navigate]);

  // ---- 终端工作台：入口在页头，面板在 TaskPage 内，二者经按 taskId 分桶的 store 协作 ----
  // 对话页的 Task：终端与会话侧栏都只属于对话区，配置 / 观测页不显示。
  const conversationTaskId = section === "conversation" ? routeTaskId : null;
  // 右侧停靠区：文件、内置浏览器、工作台三选一，各自独立（见 useDockPanels / lib/layout/dock-store）。
  const dock = useDockPanels({ workspaceId: workbenchWorkspaceId, filesVisible, conversationTaskId });
  const terminalPanelOpen = useTerminalWorkbenchStore((state) => conversationTaskId === null ? false : selectTerminalTask(conversationTaskId)(state).open);
  const terminalBadge = useTerminalBadge(conversationTaskId);
  const toggleTerminal = useCallback(() => {
    if (conversationTaskId !== null) useTerminalWorkbenchStore.getState().toggle(conversationTaskId);
  }, [conversationTaskId]);
  useHotkeys(APP_SHORTCUTS.toggleTerminal.keys, toggleTerminal, { ...APP_HOTKEY_OPTIONS, enabled: conversationTaskId !== null }, [toggleTerminal]);

  // ---- 会话侧栏：开关、摆放、页头角标与快捷键见 useTaskRail ----
  const rail = useTaskRail(conversationTaskId);
  // 只在对话页、侧栏打开时给对话视口一个附注；崩溃兜底占网格右列，与固定时的位置一致。
  const conversationAside = useMemo<ConversationAside | null>(
    () => conversationTaskId === null || !rail.open ? null : (viewportRef) => (
      <IsolatedRegion
        region="task-rail"
        label="会话侧栏"
        resetKey={`task-rail:${conversationTaskId}`}
        fallbackClassName="dock-panel bg-background col-start-2 row-start-1 m-2 w-(--task-rail-width) self-start p-3"
        escape={{ label: "收起会话侧栏", onClick: rail.close }}
      >
        <TaskRail taskId={conversationTaskId} viewportRef={viewportRef} onClose={rail.close} />
      </IsolatedRegion>
    ),
    [conversationTaskId, rail.open, rail.close],
  );

  // ---- 命令面板（⌘K）：搜索任务、开始新对话、跳转配置 ----
  const [paletteOpen, setPaletteOpen] = useState(false);
  useHotkeys(APP_SHORTCUTS.commandPalette.keys, () => setPaletteOpen(true), APP_HOTKEY_OPTIONS);

  // ---- 导航意图 ----
  // 开始新对话的唯一入口（侧栏「新对话」与工作区「+」、⌘N、命令面板）：带工作区时先显式选中它，
  // 不带时沿用当前选择。选中工作区只经这类显式意图改变，点侧栏工作区行不会改。
  const { selectWorkspace } = catalog;
  const startNewConversation = useCallback((workspaceId?: number) => {
    if (workspaceId !== undefined) selectWorkspace(workspaceId);
    navigate("/");
  }, [navigate, selectWorkspace]);
  startNewConversationRef.current = startNewConversation;
  const selectTask = useCallback((task: { task_id: number }) => {
    navigate(`/tasks/${task.task_id}`);
  }, [navigate]);
  const conversationPath = keptTaskId === null ? "/" : `/tasks/${keptTaskId}`;

  const crumbs = useMemo<Crumb[]>(() => {
    if (section === "settings") {
      const tab = findSettingsTab(SYSTEM_SETTINGS_TABS, params.tab);
      return [{ label: "系统配置", to: "/settings" }, { label: tab?.label ?? "未知分类" }];
    }
    if (section === "workspace-settings") {
      const workspace = catalog.workspaces.find((candidate) => candidate.workspace_id === workspaceConfigurationId);
      const tab = findSettingsTab(WORKSPACE_SETTINGS_TABS, params.tab);
      return [
        { label: workspace?.name ?? "工作区", to: conversationPath },
        { label: "工作区配置", to: `/workspaces/${workspaceConfigurationId}/settings` },
        { label: tab?.label ?? "未知分类" },
      ];
    }
    if (section === "observer") return [{ label: "上下文观测" }];
    if (routeTaskId === null) return [{ label: "新对话" }];
    return [
      { label: catalog.activeWorkspace?.name ?? "工作区" },
      { label: catalog.activeTask?.title || "对话" },
    ];
  }, [catalog.activeTask?.title, catalog.activeWorkspace?.name, catalog.workspaces, conversationPath, params.tab, routeTaskId, section, workspaceConfigurationId]);

  const showTaskLayer = section === "conversation" && routeTaskId !== null;
  const outletContext = useMemo<AppOutletContext>(() => ({ catalog, actions }), [actions, catalog]);

  // 侧栏开关只有一份状态，同一时刻只渲染在此刻左上角那一行：侧栏可见（停靠或抽屉）时在侧栏头部，否则在页面头部。
  const sidebarControls = <SidebarControls open={sidebarOpen} onToggle={toggleSidebar} onNewConversation={() => startNewConversation()} />;
  const sidebar = (headerControls: ReactNode) => (
    <AppSidebar
      headerControls={headerControls}
      catalog={catalog}
      focusedTaskId={focusedTaskId}
      section={section}
      onNewConversation={startNewConversation}
      onSelectTask={selectTask}
      onOpenSettings={() => navigate("/settings")}
      onOpenWorkspaceSettings={(workspaceId) => navigate(`/workspaces/${workspaceId}/settings`)}
      onOpenObserver={() => navigate("/context-observer", { state: { returnTo: focusedTaskId ? `/tasks/${focusedTaskId}` : undefined } })}
      onRequestDelete={(target) => void actions.requestDelete(target)}
    />
  );

  // 引用跳转与工作台共用同一个工作区来源：文件标签总是打开到当前显示的那个工作台桶里。
  return (
    <ReferenceBridgeProvider workspaceRoot={workbenchWorkspace?.root_path ?? ""} workspaceName={workbenchWorkspace?.name ?? "工作区"} target={filesVisible ? (routeTaskId ? `task:${routeTaskId}` : `new:${catalog.selectedWorkspaceId}`) : null} workspaceId={workbenchWorkspaceId}>
    <WorkbenchProvider workspaceId={workbenchWorkspaceId}>
      {/* 根是下沉的窗口底色，侧栏直接铺在上面；主区与停靠卡片在 bg-background 上，二者靠明度差分隔，不画线。 */}
      <div className="bg-canvas relative flex h-dvh min-h-0 overflow-hidden">
        <BackendStatusBanner />
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
        {/* 浏览器铺满时对话区让出整行；用 hidden 而不是卸载：任务页与 Assistant runtime 保持挂载。 */}
        <main className={cn("flex min-h-0 min-w-0 flex-1 flex-col", dock.maximized && "hidden")}>
          <PageHeader
            leading={sidebarOpen ? undefined : sidebarControls}
            crumbs={crumbs}
            status={showTaskLayer ? <TaskRunStatusBadge status={activeTaskStatus} /> : undefined}
            actions={(
              <>
                {conversationTaskId !== null && (
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
                    {terminalBadge.tone === "attention" && <StatusDot tone="attention" />}
                  </TooltipIconButton>
                )}
                {conversationTaskId !== null && desktopHost !== null && (
                  <TooltipIconButton
                    tooltip={[dock.panel === "browser" ? "收起浏览器" : "打开浏览器", dock.conversationTabCount > 0 && `${dock.conversationTabCount} 个标签页`].filter(Boolean).join(" · ")}
                    aria-label={`浏览器，${dock.conversationTabCount} 个标签页，${dock.panel === "browser" ? "点击收起" : "点击打开"}`}
                    aria-pressed={dock.panel === "browser"}
                    className={cn("size-7", dock.panel === "browser" && "bg-muted text-foreground")}
                    onClick={() => dock.toggle("browser")}
                  >
                    <GlobeIcon />
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
                {conversationTaskId !== null && (
                  <TooltipIconButton
                    ref={rail.buttonRef}
                    tooltip={withShortcut([rail.open ? "收起会话侧栏" : "打开会话侧栏", rail.runningCount > 0 && `${rail.runningCount} 个后台任务运行中`].filter(Boolean).join(" · "), "toggleTaskRail")}
                    aria-label={`会话侧栏，${rail.runningCount} 个后台任务运行中`}
                    aria-pressed={rail.open}
                    className={cn("relative size-7", rail.open && "bg-muted text-foreground")}
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
            `--task-rail-width` 与侧栏取同一个宽度常量。
          */}
          <div style={{ "--task-rail-width": `${TASK_RAIL_WIDTH}px` } as CSSProperties} className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
            {focusedTaskId !== null && (
              <div className="h-full" inert={!showTaskLayer} aria-hidden={!showTaskLayer || undefined}>
                <ConversationAsideContext.Provider value={conversationAside}>
                <ReferenceTargetScope.Provider value={`task:${focusedTaskId}`}><TaskPage
                  key={focusedTaskId}
                  taskId={focusedTaskId}
                  initialTask={catalog.activeTask}
                  workspaceRoot={catalog.activeWorkspace?.root_path}
                  forkingMessageId={actions.forkingMessageId}
                  onForkMessage={(messageId) => void actions.forkMessage(messageId)}
                  onTaskLoaded={catalog.handleTaskLoaded}
                  onTaskStateChanged={() => {
                    if (catalog.activeTaskWorkspaceId !== null) void catalog.refreshWorkspaceTasks(catalog.activeTaskWorkspaceId);
                  }}
                /></ReferenceTargetScope.Provider>
                </ConversationAsideContext.Provider>
              </div>
            )}
            {!showTaskLayer && (
              <div className={cn("bg-background h-full", focusedTaskId !== null && "absolute inset-0 z-10")}>
                <IsolatedRegion
                  region={section}
                  label={crumbs.at(-1)?.label ?? "页面"}
                  resetKey={location.pathname}
                  fallbackClassName="flex h-full items-center justify-center p-6"
                  escape={section === "conversation" ? undefined : { label: "返回对话", onClick: () => navigate(conversationPath) }}
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
            <BrowserPanel taskId={dock.browserTaskId} sizing={dock.sizing}
              maximized={dock.maximized} onToggleMaximized={dock.toggleMaximized} onClose={dock.close} />
          )}
        </IsolatedRegion>
        </div>
      </div>
      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        workspaces={catalog.workspaces}
        onSelectTask={selectTask}
        onNewConversation={() => startNewConversationRef.current()}
        onNavigate={navigate}
        onRestartBackend={canRestartBackendRuntime() ? () => confirmBackendRestart("command_palette") : undefined}
      />
    </WorkbenchProvider></ReferenceBridgeProvider>
  );
}
