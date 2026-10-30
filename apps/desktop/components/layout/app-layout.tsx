"use client";

import { ReferenceBridgeProvider, ReferenceTargetScope } from "@/components/workspace-references/reference-bridge";

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  Outlet,
  useLocation,
  useMatches,
  useNavigate,
  useOutletContext,
} from "react-router-dom";
import { FolderIcon, LayoutListIcon, PanelRightIcon, SquareTerminalIcon } from "lucide-react";
import { useHotkeys } from "react-hotkeys-hook";

import { BackendRestartButton } from "@/components/backend-restart-button";
import { TaskRail } from "@/components/task-rail/task-rail";
import { useTaskRail } from "@/components/task-rail/use-task-rail";
import { TASK_RAIL_WIDTH } from "@/lib/task-rail/placement";
import { CommandPalette } from "@/components/command-palette/command-palette";
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
import { CountBadge } from "@/components/ui/count-badge";
import { PanelResizeHandle } from "@/components/ui/panel-resize-handle";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { Workbench } from "@/components/workbench/workbench";
import { useWorkbenchPanel } from "./use-workbench-panel";
import { useUnsavedRouteGuard } from "./use-unsaved-route-guard";
import { useMediaQuery } from "@/hooks/use-media-query";
import { APP_HOTKEY_OPTIONS, APP_SHORTCUTS, withShortcut } from "@/lib/shortcuts";
import {
  selectTerminalTask,
  useTerminalBadge,
  useTerminalWorkbenchStore,
} from "@/lib/terminal/terminal-workbench-store";
import { WorkbenchProvider } from "@/lib/workbench/context";
import { SIDEBAR_WIDTH, useLayoutPreferences } from "@/lib/layout-preferences";
import { cn } from "@/lib/utils";
import { useTaskExecutionStatus } from "@/lib/task-status/task-status-store";
import { useTaskStatusStream } from "@/lib/task-status/use-task-status-stream";

/** 路由表上每个子路由通过 handle 声明自己属于哪个区域（见 src/router.tsx）。 */
export type RouteHandle = { section: SidebarSection; files?: boolean };

type TaskRouteState = {
  initialMessage?: string;
  initialAttachments?: InitialConversationAttachment[];
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

  const catalog = useWorkspaceCatalog(focusedTaskId, workspaceConfigurationId);
  useTaskStatusStream();
  const activeTaskStatus = useTaskExecutionStatus(catalog.activeTask?.task_id);
  useUnsavedRouteGuard();
  // 工作台显示聚焦任务所在工作区的桶（无任务时为选中工作区）；归属尚在加载时暂不显示，不销毁任何内容。
  const workbenchWorkspace = catalog.workspaces.find((workspace) => workspace.workspace_id === catalog.activeTaskWorkspaceId);
  const workbenchWorkspaceId = workbenchWorkspace?.workspace_id ?? null;
  const workbench = useWorkbenchPanel(workbenchWorkspaceId, filesVisible);
  // 快捷键先于导航函数注册，经 ref 调用最新的实现。
  const startNewConversationRef = useRef<() => void>(() => undefined);
  const actions = useWorkspaceActions(catalog, focusedTaskId);

  // ---- 侧栏：宽屏停靠（可收起并记住），窄屏图标栏 + 抽屉 ----
  const narrow = useMediaQuery(NARROW_VIEWPORT_QUERY);
  const dockedCollapsed = useLayoutPreferences((state) => state.sidebarCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const sidebarRail = narrow || dockedCollapsed;
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

  // ---- 应用级快捷键（登记见 lib/shortcuts.ts）；工作台与会话侧栏的快捷键由各自的 hook 注册 ----
  useHotkeys(APP_SHORTCUTS.newConversation.keys, () => startNewConversationRef.current(), APP_HOTKEY_OPTIONS);
  useHotkeys(APP_SHORTCUTS.toggleSidebar.keys, () => toggleSidebar(), APP_HOTKEY_OPTIONS, [toggleSidebar]);
  useHotkeys(APP_SHORTCUTS.openSettings.keys, () => navigate("/settings"), APP_HOTKEY_OPTIONS, [navigate]);

  // ---- 终端工作台：入口在页头，面板在 TaskPage 内，二者经按 taskId 分桶的 store 协作 ----
  // 对话页的 Task：终端与会话侧栏都只属于对话区，配置 / 观测页不显示。
  const conversationTaskId = section === "conversation" ? routeTaskId : null;
  const terminalPanelOpen = useTerminalWorkbenchStore((state) => conversationTaskId === null ? false : selectTerminalTask(conversationTaskId)(state).open);
  const terminalBadge = useTerminalBadge(conversationTaskId);
  const toggleTerminal = useCallback(() => {
    if (conversationTaskId !== null) useTerminalWorkbenchStore.getState().toggle(conversationTaskId);
  }, [conversationTaskId]);
  useHotkeys(APP_SHORTCUTS.toggleTerminal.keys, toggleTerminal, { ...APP_HOTKEY_OPTIONS, enabled: conversationTaskId !== null }, [toggleTerminal]);

  // ---- 会话侧栏：开关、摆放、页头角标与快捷键见 useTaskRail ----
  const rail = useTaskRail(conversationTaskId);

  // ---- 命令面板（⌘K）：搜索任务、开始新对话、跳转配置 ----
  const [paletteOpen, setPaletteOpen] = useState(false);
  useHotkeys(APP_SHORTCUTS.commandPalette.keys, () => setPaletteOpen(true), APP_HOTKEY_OPTIONS);

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

  // 引用跳转与工作台共用同一个工作区来源：文件标签总是打开到当前显示的那个工作台桶里。
  return (
    <ReferenceBridgeProvider workspaceRoot={workbenchWorkspace?.root_path ?? ""} workspaceName={workbenchWorkspace?.name ?? "工作区"} target={filesVisible ? (routeTaskId ? `task:${routeTaskId}` : `new:${catalog.selectedWorkspaceId}`) : null} workspaceId={workbenchWorkspaceId}>
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
          ref={workbench.containerRef}
          className={cn(
            "bg-background flex min-h-0 min-w-0 flex-1 [--header-inset-right:var(--titlebar-inset-right)] has-[[data-workbench-dock]]:[--header-inset-right:0px]",
            sidebarRail && "[--header-inset-left:calc(var(--titlebar-inset-left)-3rem)]",
          )}
        >
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <PageHeader
            leading={sidebarRail ? <SidebarToggleButton collapsed onToggle={toggleSidebar} /> : undefined}
            crumbs={crumbs}
            status={showTaskLayer ? <TaskRunStatusBadge status={activeTaskStatus} /> : undefined}
            actions={(
              <>
                {conversationTaskId !== null && (
                  <TooltipIconButton
                    tooltip={withShortcut(
                      [terminalPanelOpen ? "收起终端" : "打开终端", terminalBadge.hint].filter(Boolean).join(" · "),
                      "toggleTerminal",
                    )}
                    aria-label={`终端，${terminalBadge.count} 个运行中会话${terminalBadge.hint ? `，${terminalBadge.hint}` : ""}，${terminalPanelOpen ? "点击收起" : "点击打开"}`}
                    aria-pressed={terminalPanelOpen}
                    className={cn("relative size-7", terminalPanelOpen && "bg-muted text-foreground")}
                    onClick={toggleTerminal}
                  >
                    <SquareTerminalIcon />
                    {(terminalBadge.count > 0 || terminalBadge.tone !== null) && (
                      <CountBadge data-terminal-badge={terminalBadge.tone ?? "none"} tone={terminalBadge.tone === "attention" ? "attention" : "default"}>
                        {terminalBadge.count > 0 ? terminalBadge.count : "!"}
                      </CountBadge>
                    )}
                  </TooltipIconButton>
                )}
                {filesVisible && workbenchWorkspaceId !== null && (
                  <TooltipIconButton
                    tooltip="浏览工作区文件"
                    aria-label="浏览工作区文件"
                    aria-pressed={workbench.filesActive}
                    className={cn("size-7", workbench.filesActive && "bg-muted text-foreground")}
                    onClick={workbench.openFiles}
                  >
                    <FolderIcon />
                  </TooltipIconButton>
                )}
                {conversationTaskId !== null && (
                  <TooltipIconButton
                    ref={rail.buttonRef}
                    tooltip={withShortcut(rail.open ? "收起会话侧栏" : "打开会话侧栏", "toggleTaskRail")}
                    aria-label={`会话侧栏，${rail.runningCount} 个后台任务运行中`}
                    aria-pressed={rail.open}
                    className={cn("relative size-7", rail.open && "bg-muted text-foreground")}
                    onClick={rail.toggle}
                  >
                    <LayoutListIcon />
                    {rail.runningCount > 0 && <CountBadge>{rail.runningCount}</CountBadge>}
                  </TooltipIconButton>
                )}
                {workbench.tabs.length > 0 && (
                  <TooltipIconButton
                    tooltip={withShortcut(workbench.open ? "收起工作台" : "打开工作台", "toggleWorkbench")}
                    aria-label={`工作台，${workbench.tabs.length} 个标签，${workbench.open ? "点击收起" : "点击打开"}`}
                    aria-pressed={workbench.open}
                    className={cn("relative size-7", workbench.open && "bg-muted text-foreground")}
                    onClick={workbench.toggle}
                  >
                    <PanelRightIcon />
                    {/* 标签数作为角标：图标化后仍能一眼看出工作台里有几样东西。 */}
                    <CountBadge>{workbench.tabs.length}</CountBadge>
                  </TooltipIconButton>
                )}
                <BackendRestartButton />
              </>
            )}
          />
          {/*
            对话层与会话侧栏同一行：侧栏固定时占一列，浮起时叠在这一行右上角（见 TaskRail）。
            崩溃兜底只能给 class，经 `--task-rail-width` 与侧栏取同一个宽度常量。
          */}
          <div ref={rail.rowRef} style={{ "--task-rail-width": `${TASK_RAIL_WIDTH}px` } as CSSProperties} className="relative flex min-h-0 flex-1 overflow-hidden">
            <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
              {focusedTaskId !== null && (
                <div className="h-full" inert={!showTaskLayer} aria-hidden={!showTaskLayer || undefined}>
                  <ReferenceTargetScope.Provider value={`task:${focusedTaskId}`}><TaskPage
                    key={focusedTaskId}
                    taskId={focusedTaskId}
                    initialTask={catalog.activeTask}
                    initialMessage={taskState?.initialMessage}
                    initialAttachments={taskState?.initialAttachments}
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
            {conversationTaskId !== null && rail.open && (
              <IsolatedRegion
                region="task-rail"
                label="会话侧栏"
                resetKey={`task-rail:${conversationTaskId}`}
                fallbackClassName="dock-panel bg-background m-2 w-(--task-rail-width) shrink-0 p-3"
                escape={{ label: "收起会话侧栏", onClick: rail.close }}
              >
                <TaskRail taskId={conversationTaskId} placement={rail.placement} onClose={rail.close} />
              </IsolatedRegion>
            )}
          </div>
        </main>

        <IsolatedRegion
          region="workbench"
          label="工作台"
          // 收起会改变 resetKey，从而清除崩溃状态；下次打开重新挂载。
          resetKey={`workbench:${workbenchWorkspaceId ?? "none"}:${workbench.open ? "open" : "closed"}`}
          fallbackClassName="dock-panel bg-background m-2 w-80 shrink-0 p-3"
          escape={{ label: "收起工作台", onClick: () => workbench.setOpen(false) }}
        >
          {workbenchWorkspace && workbench.open && (
            <Workbench workspace={workbenchWorkspace} tabs={workbench.tabs} activeTabId={workbench.activeTabId}
              width={workbench.width} minWidth={workbench.minWidth} maxWidth={workbench.maxWidth}
              onWidthChange={workbench.setWidth} onRequestClose={() => workbench.setOpen(false)} />
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
      />
    </WorkbenchProvider></ReferenceBridgeProvider>
  );
}
