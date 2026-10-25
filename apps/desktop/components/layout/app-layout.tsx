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
import { PanelRightCloseIcon, PanelRightOpenIcon } from "lucide-react";
import { useHotkeys } from "react-hotkeys-hook";

import { BackendRestartButton } from "@/components/backend-restart-button";
import { CommandPalette } from "@/components/command-palette/command-palette";
import { useCommandPaletteHotkey } from "@/components/command-palette/use-command-palette-hotkey";
import { BackendStatusBanner } from "@/components/backend-status-banner";
import { AppSidebar, type SidebarSection } from "@/components/layout/sidebar/app-sidebar";
import { PageHeader, type Crumb } from "@/components/layout/page-header";
import { useWorkspaceActions } from "@/components/layout/use-workspace-actions";
import { useWorkspaceCatalog, type WorkspaceCatalog } from "@/components/layout/use-workspace-catalog";
import type { InitialConversationAttachment } from "@/components/new-conversation";
import { IsolatedRegion } from "@/components/render-isolation/isolated-region";
import { findSettingsTab, SYSTEM_SETTINGS_TABS, WORKSPACE_SETTINGS_TABS } from "@/components/settings/settings-tabs";
import { TaskPage } from "@/components/task-page";
import { TaskRunStatusBadge } from "@/components/task-tree/task-run-status";
import { Button } from "@/components/ui/button";
import { Workbench } from "@/components/workbench";
import { useWorkbenchLayout } from "./use-workbench-layout";
import { useWorkspaceNavigation } from "./use-workspace-navigation";
import { useMediaQuery } from "@/hooks/use-media-query";
import { APP_SHORTCUTS } from "@/lib/shortcuts";
import { WorkbenchProvider } from "@/lib/workbench/context";
import { useWorkbenchStore } from "@/lib/workbench/store";
import { readSidebarCollapsed, writeSidebarCollapsed } from "@/lib/workspace-preferences";
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
  const hotkeyOptions = { enableOnFormTags: true, enableOnContentEditable: true, preventDefault: true } as const;
  useHotkeys(APP_SHORTCUTS.newConversation.keys, () => startNewConversationRef.current(), hotkeyOptions);
  useHotkeys(APP_SHORTCUTS.toggleSidebar.keys, () => toggleSidebar(), hotkeyOptions, [toggleSidebar]);
  useHotkeys(APP_SHORTCUTS.openSettings.keys, () => navigate("/settings"), hotkeyOptions, [navigate]);

  // ---- 命令面板（⌘K）：搜索任务、开始新对话、跳转配置 ----
  const [paletteOpen, setPaletteOpen] = useState(false);
  useCommandPaletteHotkey(useCallback(() => setPaletteOpen(true), []));

  // ---- 工作台 ----
  const allWorkbenchTabs = useWorkbenchStore((state) => state.tabs);
  const workbenchTabs = allWorkbenchTabs.filter(tab => filesVisible || tab.kind !== "files");
  const workbenchPanelOpen = useWorkbenchStore((state) => state.panelOpen);
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
    if (section === "observer") return [{ label: "开发者" }, { label: "上下文观测" }];
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
      <div className="bg-background relative flex h-dvh min-h-0 overflow-hidden">
        <BackendStatusBanner />
        <aside
          className={cn(
            "bg-muted/20 flex min-h-0 shrink-0 flex-col border-r",
            narrow || dockedCollapsed ? "w-14" : "w-72",
            !narrow && "transition-[width] duration-200 motion-reduce:transition-none",
          )}
        >
          {sidebar(narrow || dockedCollapsed ? "rail" : "expanded")}
        </aside>
        {narrow && drawerOpen && (
          <>
            <div className="absolute inset-0 z-40 bg-black/20" aria-hidden="true" onClick={() => setDrawerOpen(false)} />
            <aside className="bg-background absolute inset-y-0 left-0 z-50 flex w-72 min-h-0 flex-col border-r shadow-xl" aria-label="侧栏">
              {sidebar("expanded")}
            </aside>
          </>
        )}

        <div ref={workbenchLayout.containerRef} className="flex min-h-0 min-w-0 flex-1">
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <PageHeader
            crumbs={crumbs}
            status={showTaskLayer ? <TaskRunStatusBadge status={catalog.activeTask?.execution_status} /> : undefined}
            actions={(
              <>
                {filesVisible && catalog.activeWorkspace && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="border-transparent focus-visible:border-transparent"
                    aria-label="浏览工作区文件"
                    onClick={() => useWorkbenchStore.getState().openFiles(
                      catalog.activeWorkspace!.workspace_id,
                      catalog.activeWorkspace!.root_path,
                      catalog.activeWorkspace!.name,
                    )}
                  >
                    文件
                  </Button>
                )}
                {workbenchTabs.length > 0 && (
                  <Button
                    type="button"
                    variant={workbenchPanelOpen ? "secondary" : "outline"}
                    size="sm"
                    className="border-transparent focus-visible:border-transparent"
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
          fallbackClassName="w-80 shrink-0 border-l p-3"
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
