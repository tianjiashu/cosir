/**
 * 根组件：桌面工作台布局。
 *
 * 组装：
 * - TopBar（顶部任务栏）
 * - Sidebar（左侧导航栏）
 * - ChatPanel（中央主会话区）
 * - RightPanel（右侧信息面板）
 * - InputBar（底部输入区）
 * - LogsPage（日志诊断页）
 *
 * 会话页保持左 / 中 / 右三栏结构；日志页复用左侧导航栏，
 * 将中右区域替换为诊断页面，避免把日志查看塞进会话工作区。
 *
 * 三栏宽度由 react-resizable-panels 管理，用户拖拽结果按视图维度
 * 经 useDefaultLayout 持久化到 localStorage，刷新后保留上次布局。
 *
 * @module App
 */

import { useEffect, useRef, useState } from "react";
import { Group, Panel, useDefaultLayout } from "react-resizable-panels";
import { Sidebar } from "@/components/layout/Sidebar";
import { TopBar } from "@/components/layout/TopBar";
import { ChatPanel } from "@/components/layout/ChatPanel";
import { ChangesDrawer } from "@/components/layout/ChangesDrawer";
import { RightPanel } from "@/components/layout/RightPanel";
import { InputBar } from "@/components/layout/InputBar";
import { BackendErrorBanner } from "@/components/backend/BackendErrorBanner";
import { useBackendBootstrap } from "@/hooks/useBackendBootstrap";
import { useTask } from "@/hooks/useTask";
import { PanelDragHandle } from "@/components/layout/PanelDragHandle";
import { LogsPage } from "@/pages/logs/LogsPage";
import { NewTaskPage } from "@/pages/chat/NewTaskPage";
import * as api from "@/services/api";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useTaskStore, loadPersistedActiveTaskId } from "@/stores/taskStore";
import { loadWorkspaceTasks } from "@/hooks/useWorkspaceTaskLazyLoad";
import type { TaskRecord } from "@shared/task";
import { logError, logInfo } from "@/lib/logger";

/** 工作台主视图。 */
type WorkspaceView = "chat" | "new-task" | "logs";

/**
 * 布局持久化 id。
 *
 * 会话视图为三栏、其余视图为两栏，Panel 组成不同，
 * 必须分开存储，避免两种视图互相覆盖对方的栏宽。
 */
const CHAT_LAYOUT_ID = "workbench-layout-chat-v1";
const FULL_LAYOUT_ID = "workbench-layout-full-v1";

/**
 * Panel 稳定 id。
 *
 * 持久化布局以 Panel id 为键，必须显式指定且保持稳定；
 * 若交由 useId 自动生成，刷新后 id 变化会导致布局无法恢复。
 */
const SIDEBAR_PANEL_ID = "sidebar";
const CENTER_PANEL_ID = "center";
const RIGHT_PANEL_ID = "right";

/**
 * 各栏默认宽度。
 *
 * 使用像素值保持与改造前 w-60(240px) / w-72(288px) 一致的初始观感；
 * 中央区不设默认值，占据剩余空间。
 */
const SIDEBAR_DEFAULT_SIZE = "240px";
const RIGHT_PANEL_DEFAULT_SIZE = "288px";

/**
 * 各栏最小宽度。
 *
 * 以最小窗口 960px 为基准，保证极限拖拽下三栏内容仍可读，
 * 不出现被压没的空栏。
 */
const SIDEBAR_MIN_SIZE = "180px";
const RIGHT_PANEL_MIN_SIZE = "200px";
const CENTER_MIN_SIZE = "320px";

/**
 * App 根组件。
 *
 * 布局结构：
 * - 顶栏：固定高度，不参与分栏
 * - 主体：Group 水平分栏，栏宽可拖拽并持久化到 localStorage
 * - 会话视图：左侧导航 / 中央会话区 / 右侧信息面板 三栏
 * - 日志与新建任务视图：左侧导航 + 内容区 两栏
 *
 * 尺寸约定（对齐开发计划 §4.1）：
 * - 默认窗口 1200×800，最小 960×640
 * - 左侧 ~240px，右侧 ~288px，中央弹性；用户拖拽后以持久化值为准
 */
export default function App() {
  useBackendBootstrap();
  const [activeView, setActiveView] = useState<WorkspaceView>("chat");
  const setWorkspaces = useWorkspaceStore((s) => s.setWorkspaces);
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  const { openTask } = useTask();
  // 防止重复触发自动恢复：仅首个工作区加载完成时尝试恢复一次。
  const resumeAttempted = useRef(false);

  // 布局持久化：两种视图的 Panel 组成不同，各自独立存取。
  // Hook 不可条件调用，故两个 layout 均在顶层获取，由渲染分支择一使用。
  const chatLayout = useDefaultLayout({
    id: CHAT_LAYOUT_ID,
    panelIds: [SIDEBAR_PANEL_ID, CENTER_PANEL_ID, RIGHT_PANEL_ID],
    onlySaveAfterUserInteractions: true,
  });
  const fullLayout = useDefaultLayout({
    id: FULL_LAYOUT_ID,
    panelIds: [SIDEBAR_PANEL_ID, CENTER_PANEL_ID],
    onlySaveAfterUserInteractions: true,
  });

  useEffect(() => {
    let cancelled = false;
    async function loadWorkspaceState() {
      try {
        const workspaces = await api.listWorkspaces();
        if (!cancelled) {
          // 不自动创建默认工作区：列表为空时进入「无工作区」引导态，
          // 由 ChatPanel 空状态与新建任务页引导用户主动选择项目目录。
          setWorkspaces(workspaces);
        }
      } catch (err) {
        logError("加载工作区失败", err, { module: "App" });
      }
    }
    void loadWorkspaceState();
    return () => {
      cancelled = true;
    };
  }, [setWorkspaces]);

  // openTask 经 ref 持有最新引用，使其不进入 effect 依赖数组：
  // 避免 useTask 内部依赖变化导致本 effect 误重跑。effect 触发条件严格等于
  // 「仅 activeWorkspaceId 变化」（启动 / 增删工作区）；点 workspace 展开/折叠
  // 不触发（Sidebar 不再改 activeWorkspaceId），「点 workspace 不切走中央对话」诉求天然满足。
  const openTaskRef = useRef(openTask);
  openTaskRef.current = openTask;

  useEffect(() => {
    let cancelled = false;
    async function loadTasks() {
      if (!activeWorkspaceId) {
        return;
      }
      // 复用权威 loader（与 Sidebar 共用模块级去重，不重复发起 HTTP）；
      // 加载态由 taskStore.loadedWorkspaceIds 维护，与数据态正交。
      // loader 内部已捕获失败并记日志，成功才返回 true；失败时 tasks 保持空，
      // 首屏恢复分支据此降级（不消费 resumeAttempted），下次 activeWorkspaceId
      // 变化时可重试，不静默吞掉恢复机会。
      // 先快照持久化活跃任务：后续 await 期间可能被其它路径（如新建任务 addTask）
      // 改写 activeTaskId，提前固定用于首屏恢复判定，避免被覆盖成"列表首项"。
      const persistedId = loadPersistedActiveTaskId();
      let tasks: TaskRecord[] = [];
      const loaded = await loadWorkspaceTasks(activeWorkspaceId);
      if (loaded && !cancelled) {
        tasks = useTaskStore.getState().tasksByWorkspaceId[activeWorkspaceId] ?? [];
      }
      // 首屏自动恢复活跃任务并回放历史（仅首次且本次加载成功）；加载失败时不消费
      // resumeAttempted，留给后续重试。选中契约与 Sidebar 点击一致：统一经 openTask
      // （选中 + 拉历史回放），不在 store 层隐式选中后留空白。优先级：持久化任务 > 列表首项；
      // 二者皆无则不回放（无任务可显示）。持久化脏值清理由 taskStore 单点负责。
      if (loaded && !cancelled && !resumeAttempted.current) {
        resumeAttempted.current = true;
        const resumeTaskId = persistedId && tasks.some((task) => task.task_id === persistedId)
          ? persistedId
          : tasks[0]?.task_id ?? null;
        if (resumeTaskId) {
          logInfo("首屏自动恢复活跃任务并回放历史", {
            module: "App",
            task_id: resumeTaskId,
            from_persisted: resumeTaskId === persistedId,
          });
          await openTaskRef.current(resumeTaskId);
        }
      }
    }
    void loadTasks();
    return () => {
      cancelled = true;
    };
  }, [activeWorkspaceId]);

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-background text-foreground">
      {/* 顶部任务栏 */}
      <TopBar />

      {/* 后端启动失败错误横幅 */}
      <BackendErrorBanner onViewLogs={() => setActiveView("logs")} />

      {/* 可拖拽分栏主体：会话视图三栏，其余视图两栏 */}
      {activeView === "chat" ? (
        // key 按视图区分：Panel 组成变化时强制重建 Group，
        // 避免库复用上一视图的 layout 导致栏宽错乱。
        <Group
          key={CHAT_LAYOUT_ID}
          id={CHAT_LAYOUT_ID}
          orientation="horizontal"
          className="flex-1 overflow-hidden"
          defaultLayout={chatLayout.defaultLayout}
          onLayoutChanged={chatLayout.onLayoutChanged}
        >
          <Panel
            id={SIDEBAR_PANEL_ID}
            defaultSize={SIDEBAR_DEFAULT_SIZE}
            minSize={SIDEBAR_MIN_SIZE}
            className="overflow-hidden"
          >
            <Sidebar
              activeView={activeView}
              onOpenLogs={() => setActiveView("logs")}
              onOpenChat={() => setActiveView("chat")}
              onNewTask={() => setActiveView("new-task")}
            />
          </Panel>

          <PanelDragHandle ariaLabel="调整左侧导航栏宽度" />

          <Panel id={CENTER_PANEL_ID} minSize={CENTER_MIN_SIZE} className="min-w-0 overflow-hidden">
            {/* 中央主会话区 + 底部输入区 */}
            <div className="flex h-full min-w-0 flex-col overflow-hidden">
              <ChatPanel onPickWorkspace={() => setActiveView("new-task")} />
              <ChangesDrawer />
              <InputBar />
            </div>
          </Panel>

          <PanelDragHandle ariaLabel="调整右侧信息面板宽度" />

          <Panel
            id={RIGHT_PANEL_ID}
            defaultSize={RIGHT_PANEL_DEFAULT_SIZE}
            minSize={RIGHT_PANEL_MIN_SIZE}
            className="overflow-hidden"
          >
            <RightPanel />
          </Panel>
        </Group>
      ) : (
        <Group
          key={FULL_LAYOUT_ID}
          id={FULL_LAYOUT_ID}
          orientation="horizontal"
          className="flex-1 overflow-hidden"
          defaultLayout={fullLayout.defaultLayout}
          onLayoutChanged={fullLayout.onLayoutChanged}
        >
          <Panel
            id={SIDEBAR_PANEL_ID}
            defaultSize={SIDEBAR_DEFAULT_SIZE}
            minSize={SIDEBAR_MIN_SIZE}
            className="overflow-hidden"
          >
            <Sidebar
              activeView={activeView}
              onOpenLogs={() => setActiveView("logs")}
              onOpenChat={() => setActiveView("chat")}
              onNewTask={() => setActiveView("new-task")}
            />
          </Panel>

          <PanelDragHandle ariaLabel="调整左侧导航栏宽度" />

          <Panel id={CENTER_PANEL_ID} minSize={CENTER_MIN_SIZE} className="min-w-0 overflow-hidden">
            {activeView === "logs" ? (
              <LogsPage onBack={() => setActiveView("chat")} />
            ) : (
              <NewTaskPage onCreated={() => setActiveView("chat")} />
            )}
          </Panel>
        </Group>
      )}
    </div>
  );
}
