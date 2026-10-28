"use client";

import { ReferenceBridgeProvider, ReferenceTargetScope } from "@/components/workspace-references/reference-bridge";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Outlet,
  useLocation,
  useMatches,
  useNavigate,
  useOutletContext,
} from "react-router-dom";
import { FolderIcon, PanelRightIcon, SquareTerminalIcon } from "lucide-react";
import { useHotkeys } from "react-hotkeys-hook";

import { BackendRestartButton } from "@/components/backend-restart-button";
import { CommandPalette } from "@/components/command-palette/command-palette";
import { useCommandPaletteHotkey } from "@/components/command-palette/use-command-palette-hotkey";
import { BackendStatusBanner } from "@/components/backend-status-banner";
import { AppSidebar, SidebarToggleButton, type SidebarSection } from "@/components/layout/sidebar/app-sidebar";
import { PageHeader, type Crumb } from "@/components/layout/page-header";
import { useWorkspaceActions } from "@/components/layout/use-workspace-actions";
import { useWorkspaceCatalog, type WorkspaceCatalog } from "@/components/layout/use-workspace-catalog";
import type { InitialConversationAttachment } from "@/components/new-conversation";
import { IsolatedRegion } from "@/components/render-isolation/isolated-region";
import { findSettingsTab, SYSTEM_SETTINGS_TABS, WORKSPACE_SETTINGS_TABS } from "@/components/settings/settings-tabs";
import { TaskPage } from "@/components/task-page";
import { TaskRunStatusBadge } from "@/components/task-tree/task-run-status";
import { PanelResizeHandle } from "@/components/ui/panel-resize-handle";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { Workbench } from "@/components/workbench";
import { useWorkbenchLayout } from "./use-workbench-layout";
import { useWorkspaceNavigation } from "./use-workspace-navigation";
import { useMediaQuery } from "@/hooks/use-media-query";
import { APP_SHORTCUTS, isTerminalKeyEvent, withShortcut } from "@/lib/shortcuts";
import {
  selectActiveSessionCount,
  selectTerminalTask,
  useTerminalWorkbenchStore,
} from "@/lib/terminal/terminal-workbench-store";
import { WorkbenchProvider } from "@/lib/workbench/context";
import { useWorkbenchStore } from "@/lib/workbench/store";
import {
  clampSidebarWidth,
  readSidebarCollapsed,
  readSidebarWidth,
  SIDEBAR_WIDTH,
  writeSidebarCollapsed,
  writeSidebarWidth,
} from "@/lib/workspace-preferences";
import { cn } from "@/lib/utils";

/** 路由表上每个子路由通过 handle 声明自己属于哪个区域（见 src/router.tsx）。 */
export type RouteHandle = { section: SidebarSection; files?: boolean };

type TaskRouteState = {
  initialMessage?: string;
  initialAttachments?: InitialConversationAttachment[];
  initialDisabledToolGroups?: string[];
  initialBanTools?: string[];
};

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
 * - 窄屏（≤1024px）侧栏为图标栏，展开时以抽屉覆盖主区域；宽屏停靠，收起偏好持久化。
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

  // 最近一个任务：对话区为当前路由任务；其余区域沿用上一次的任务（保活）。对话区的值必须在
  // 渲染时同步得出——用 effect 回填会让新挂载的任务页先拿到旧的路由 state，丢掉首条消息。
  const [keptTaskId, setKeptTaskId] = useState<number | null>(routeTaskId);
  const [keptTaskState, setKeptTaskState] = useState<TaskRouteState | null>(null);
  const routeTaskState = section === "conversation" && routeTaskId !== null
    ? (location.state as TaskRouteState | null)
    : null;
  useEffect(() => {
    if (section !== "conversation") return;
    setKeptTaskId(routeTaskId);
    setKeptTaskState(routeTaskState);
  }, [routeTaskId, routeTaskState, section]);
  const filesVisible = Boolean((leaf?.handle as RouteHandle | undefined)?.files) && (!params.taskId || routeTaskId !== null);
  const focusedTaskId = section === "conversation" ? routeTaskId : keptTaskId;
  const taskState = section === "conversation" ? routeTaskState : keptTaskState;

  const rawCatalog = useWorkspaceCatalog(focusedTaskId, workspaceConfigurationId);
  const selectWorkspace = useWorkspaceNavigation(rawCatalog, focusedTaskId);
  const catalog = useMemo(() => ({ ...rawCatalog, selectWorkspace }), [rawCatalog, selectWorkspace]);
  const currentWorkbenchWorkspaceId = useWorkbenchStore(state => state.workspaceId);
  // 未知 task 的归属仍在加载时，null 不是离开当前工作区的意图，不销毁已有草稿。
  const workbenchWorkspaceId = catalog.activeTaskWorkspaceId
    ?? (focusedTaskId !== null ? currentWorkbenchWorkspaceId : null);
  const workbenchLayout = useWorkbenchLayout();
  // 快捷键先于导航函数注册，经 ref 调用最新的实现。
  const startNewConversationRef = useRef<() => void>(() => undefined);
  const actions = useWorkspaceActions(catalog, focusedTaskId);

  // ---- 侧栏：宽屏停靠（可收起并记住），窄屏图标栏 + 抽屉 ----
  const narrow = useMediaQuery(NARROW_VIEWPORT_QUERY);
  const [dockedCollapsed, setDockedCollapsed] = useState(readSidebarCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const sidebarRail = narrow || dockedCollapsed;
  // 展开宽度是用户偏好：拖拽右缘调整，宽屏停靠与窄屏抽屉共用。
  const [sidebarWidth, setSidebarWidth] = useState(readSidebarWidth);
  const resizeSidebar = useCallback((width: number) => setSidebarWidth(clampSidebarWidth(width)), []);
  useEffect(() => writeSidebarWidth(sidebarWidth), [sidebarWidth]);
  const toggleSidebar = useCallback(() => {
    if (narrow) {
      setDrawerOpen((open) => !open);
      return;
    }
    setDockedCollapsed((collapsed) => {
      writeSidebarCollapsed(!collapsed);
      return !collapsed;
    });
  }, [narrow]);
  useEffect(() => {
    if (!narrow) setDrawerOpen(false);
  }, [narrow]);
  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);
  useHotkeys("escape", () => setDrawerOpen(false), { enabled: drawerOpen, enableOnFormTags: true });

  // ---- 应用级快捷键（登记见 lib/shortcuts.ts）。输入框内同样生效：这些组合键不与文本编辑冲突。 ----
  const hotkeyOptions = { enableOnFormTags: true, enableOnContentEditable: true, preventDefault: true, ignoreEventWhen: isTerminalKeyEvent } as const;
  useHotkeys(APP_SHORTCUTS.newConversation.keys, () => startNewConversationRef.current(), hotkeyOptions);
  useHotkeys(APP_SHORTCUTS.toggleSidebar.keys, () => toggleSidebar(), hotkeyOptions, [toggleSidebar]);
  useHotkeys(APP_SHORTCUTS.openSettings.keys, () => navigate("/settings"), hotkeyOptions, [navigate]);

  // ---- 终端工作台：入口在页头，面板在 TaskPage 内，二者经按 taskId 分桶的 store 协作 ----
  const terminalTaskId = section === "conversation" ? routeTaskId : null;
  const terminalPanelOpen = useTerminalWorkbenchStore((state) => terminalTaskId === null ? false : selectTerminalTask(terminalTaskId)(state).open);
  const terminalSessionCount = useTerminalWorkbenchStore((state) => terminalTaskId === null ? 0 : selectActiveSessionCount(terminalTaskId)(state));
  const toggleTerminal = useCallback(() => {
    if (terminalTaskId !== null) useTerminalWorkbenchStore.getState().toggle(terminalTaskId);
  }, [terminalTaskId]);
  useHotkeys(APP_SHORTCUTS.toggleTerminal.keys, toggleTerminal, { ...hotkeyOptions, enabled: terminalTaskId !== null }, [toggleTerminal]);

  // ---- 命令面板（⌘K）：搜索任务、开始新对话、跳转配置 ----
  const [paletteOpen, setPaletteOpen] = useState(false);
  useCommandPaletteHotkey(useCallback(() => setPaletteOpen(true), []));

  // ---- 工作台 ----
  const allWorkbenchTabs = useWorkbenchStore((state) => state.tabs);
  const workbenchTabs = allWorkbenchTabs.filter(tab => filesVisible || tab.kind !== "files");
  const workbenchPanelOpen = useWorkbenchStore((state) => state.panelOpen);
  const workbenchActiveTabId = useWorkbenchStore((state) => state.activeTabId);
  // 顶栏图标按当前打开的面板高亮：文件标签在前台时「文件」亮，工作台打开时「工作台」亮。
  const filesPanelActive = workbenchPanelOpen && workbenchTabs.some((tab) => tab.kind === "files" && tab.id === workbenchActiveTabId);
  const setWorkbenchPanelOpen = useWorkbenchStore((state) => state.setPanelOpen);
  // 收起只改变可见性；草稿由工作台会话保留，真正销毁由关闭/工作区切换边界确认。
  const toggleWorkbenchPanel = useCallback(() => {
    setWorkbenchPanelOpen(!workbenchPanelOpen);
  }, [setWorkbenchPanelOpen, workbenchPanelOpen]);

  // ---- 导航意图 ----
  const startNewConversation = useCallback(() => {
    navigate("/");
  }, [navigate]);
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

  const sidebar = (variant: "rail" | "expanded") => (
    <AppSidebar
      catalog={catalog}
      focusedTaskId={focusedTaskId}
      section={section}
      variant={variant}
      onToggle={toggleSidebar}
      onNewConversation={startNewConversation}
      onSelectTask={selectTask}
      onOpenSettings={() => navigate("/settings")}
      onOpenWorkspaceSettings={(workspaceId) => navigate(`/workspaces/${workspaceId}/settings`)}
      onOpenObserver={() => navigate("/context-observer", { state: { returnTo: focusedTaskId ? `/tasks/${focusedTaskId}` : undefined } })}
      onRequestDelete={(target) => void actions.requestDelete(target)}
    />
  );

  return (
    <ReferenceBridgeProvider workspaceRoot={catalog.activeWorkspace?.root_path ?? ""} workspaceName={catalog.activeWorkspace?.name ?? "工作区"} target={filesVisible ? (routeTaskId ? `task:${routeTaskId}` : `new:${catalog.selectedWorkspaceId}`) : null} workspaceId={catalog.activeWorkspace?.workspace_id ?? null}>
    <WorkbenchProvider workspaceId={workbenchWorkspaceId}>
      {/* 根是下沉的窗口底色，侧栏直接铺在上面；主区与停靠卡片在 bg-background 上，二者靠明度差分隔，不画线。 */}
      <div className="bg-canvas relative flex h-dvh min-h-0 overflow-hidden">
        <BackendStatusBanner />
        <aside
          style={sidebarRail ? undefined : { width: sidebarWidth }}
          className={cn(
            "relative flex min-h-0 shrink-0 flex-col",
            sidebarRail && "w-14",
            // 收起 / 展开有过渡；拖拽调宽时关掉，否则宽度追着指针慢半拍。
            !narrow && "transition-[width] duration-200 motion-reduce:transition-none [&:has(>[role=separator]:active)]:transition-none",
          )}
        >
          {sidebar(sidebarRail ? "rail" : "expanded")}
          {!sidebarRail && (
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
        {narrow && drawerOpen && (
          <>
            <div className="absolute inset-0 z-40 bg-black/20" aria-hidden="true" onClick={() => setDrawerOpen(false)} />
            <aside style={{ width: sidebarWidth }} className="bg-canvas absolute inset-y-0 left-0 z-50 flex min-h-0 flex-col shadow-xl" aria-label="侧栏">
              {sidebar("expanded")}
            </aside>
          </>
        )}

        {/*
          页面头部让位：右上角有工作台卡片时由卡片标签行让位，否则头部让位（以 DOM 中是否
          渲染了工作台为准，工作台自身还有多种不渲染的情形）；侧栏为 56px 图标栏时红绿灯越过
          图标栏，头部左侧补足差值。安全区为 0 的平台上两者都退回头部默认内边距。
        */}
        <div
          ref={workbenchLayout.containerRef}
          className={cn(
            "bg-background flex min-h-0 min-w-0 flex-1 [--header-inset-right:var(--titlebar-inset-right)] has-[[data-workbench-dock]]:[--header-inset-right:0px]",
            sidebarRail && "[--header-inset-left:calc(var(--titlebar-inset-left)-3rem)]",
          )}
        >
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <PageHeader
            leading={sidebarRail ? <SidebarToggleButton collapsed onToggle={toggleSidebar} /> : undefined}
            crumbs={crumbs}
            status={showTaskLayer ? <TaskRunStatusBadge status={catalog.activeTask?.execution_status} /> : undefined}
            actions={(
              <>
                {terminalTaskId !== null && (
                  <TooltipIconButton
                    tooltip={withShortcut(terminalPanelOpen ? "收起终端" : "打开终端", "toggleTerminal")}
                    aria-label={`终端，${terminalSessionCount} 个运行中会话，${terminalPanelOpen ? "点击收起" : "点击打开"}`}
                    aria-pressed={terminalPanelOpen}
                    className={cn("relative size-7", terminalPanelOpen && "bg-muted text-foreground")}
                    onClick={toggleTerminal}
                  >
                    <SquareTerminalIcon />
                    {terminalSessionCount > 0 && (
                      <span aria-hidden="true" className="bg-foreground text-background absolute -top-0.5 -right-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full px-0.5 text-2xs leading-none tabular-nums">{terminalSessionCount}</span>
                    )}
                  </TooltipIconButton>
                )}
                {filesVisible && catalog.activeWorkspace && (
                  <TooltipIconButton
                    tooltip="浏览工作区文件"
                    aria-label="浏览工作区文件"
                    aria-pressed={filesPanelActive}
                    className={cn("size-7", filesPanelActive && "bg-muted text-foreground")}
                    onClick={() => useWorkbenchStore.getState().openFiles(
                      catalog.activeWorkspace!.workspace_id,
                      catalog.activeWorkspace!.root_path,
                      catalog.activeWorkspace!.name,
                    )}
                  >
                    <FolderIcon />
                  </TooltipIconButton>
                )}
                {workbenchTabs.length > 0 && (
                  <TooltipIconButton
                    tooltip={workbenchPanelOpen ? "收起工作台" : "打开工作台"}
                    aria-label={`工作台，${workbenchTabs.length} 个标签，${workbenchPanelOpen ? "点击收起" : "点击打开"}`}
                    aria-pressed={workbenchPanelOpen}
                    className={cn("relative size-7", workbenchPanelOpen && "bg-muted text-foreground")}
                    onClick={toggleWorkbenchPanel}
                  >
                    <PanelRightIcon />
                    {/* 标签数作为角标：图标化后仍能一眼看出工作台里有几样东西。 */}
                    <span aria-hidden="true" className="bg-foreground text-background absolute -top-0.5 -right-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full px-0.5 text-2xs leading-none tabular-nums">{workbenchTabs.length}</span>
                  </TooltipIconButton>
                )}
                <BackendRestartButton />
              </>
            )}
          />
          <div className="relative min-h-0 flex-1 overflow-hidden">
            {focusedTaskId !== null && (
              <div className="h-full" inert={!showTaskLayer} aria-hidden={!showTaskLayer || undefined}>
                <ReferenceTargetScope.Provider value={`task:${focusedTaskId}`}><TaskPage
                  key={focusedTaskId}
                  taskId={focusedTaskId}
                  initialTask={catalog.activeTask}
                  initialMessage={taskState?.initialMessage}
                  initialAttachments={taskState?.initialAttachments}
                  initialDisabledToolGroups={taskState?.initialDisabledToolGroups}
                  initialBanTools={taskState?.initialBanTools}
                  workspaceRoot={catalog.activeWorkspace?.root_path}
                  forkingMessageId={actions.forkingMessageId}
                  onForkMessage={(messageId) => void actions.forkMessage(messageId)}
                  onTaskLoaded={catalog.handleTaskLoaded}
                  onTaskStateChanged={() => {
                    if (catalog.activeTaskWorkspaceId !== null) void catalog.refreshWorkspaceTasks(catalog.activeTaskWorkspaceId);
                  }}
                /></ReferenceTargetScope.Provider>
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
          region="workbench"
          label="工作台"
          // 收起会改变 resetKey，从而清除崩溃状态；下次打开重新挂载。
          resetKey={`workbench:${workbenchWorkspaceId ?? "none"}:${workbenchPanelOpen ? "open" : "closed"}`}
          fallbackClassName="dock-panel bg-background m-2 w-80 shrink-0 p-3"
          escape={{ label: "收起工作台", onClick: () => setWorkbenchPanelOpen(false) }}
        >
          <Workbench filesVisible={filesVisible} workspaceId={workbenchWorkspaceId}
            width={workbenchLayout.width} minWidth={workbenchLayout.minWidth} maxWidth={workbenchLayout.maxWidth}
            onWidthChange={workbenchLayout.setWidth} onRequestClose={() => setWorkbenchPanelOpen(false)} />
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
      />
    </WorkbenchProvider></ReferenceBridgeProvider>
  );
}
