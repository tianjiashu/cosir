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
 * @module App
 */

import { useEffect, useRef, useState } from "react";
import { Sidebar } from "@/components/layout/Sidebar";
import { TopBar } from "@/components/layout/TopBar";
import { ChatPanel } from "@/components/layout/ChatPanel";
import { RightPanel } from "@/components/layout/RightPanel";
import { InputBar } from "@/components/layout/InputBar";
import { BackendErrorBanner } from "@/components/backend/BackendErrorBanner";
import { useBackendBootstrap } from "@/hooks/useBackendBootstrap";
import { useTask } from "@/hooks/useTask";
import { LogsPage } from "@/pages/logs/LogsPage";
import { NewTaskPage } from "@/pages/chat/NewTaskPage";
import * as api from "@/services/api";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useTaskStore, loadPersistedActiveTaskId } from "@/stores/taskStore";
import { logError } from "@/lib/logger";

/** 工作台主视图。 */
type WorkspaceView = "chat" | "new-task" | "logs";

/**
 * App 根组件。
 *
 * 使用 flex 实现工作台布局：
 * - 顶栏：固定高度
 * - 主体：左侧导航固定宽度，右侧内容按当前视图切换
 * - 会话视图：中央会话区弹性宽度，右侧信息面板固定宽度
 * - 日志视图：诊断页占用导航栏右侧全部空间
 *
 * 尺寸约定（对齐开发计划 §4.1）：
 * - 默认窗口 1200×800，最小 960×640
 * - 左侧 ~240px，右侧 ~280px，中央弹性
 */
export default function App() {
  useBackendBootstrap();
  const [activeView, setActiveView] = useState<WorkspaceView>("chat");
  const setWorkspaces = useWorkspaceStore((s) => s.setWorkspaces);
  const activeWorkspaceId = useWorkspaceStore((s) => s.activeWorkspaceId);
  const setTasks = useTaskStore((s) => s.setTasks);
  const { openTask } = useTask();
  // 防止重复触发自动恢复：仅首个工作区加载完成时尝试恢复一次。
  const resumeAttempted = useRef(false);

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

  useEffect(() => {
    let cancelled = false;
    async function loadTasks() {
      if (!activeWorkspaceId) {
        setTasks([]);
        return;
      }
      // 必须在 setTasks 之前快照持久化活跃任务：setTasks 的回退分支会改写
      // activeTaskId，若事后才读会被覆盖成"列表首项"而非"上次活跃任务"。
      const persistedId = loadPersistedActiveTaskId();
      let tasks: Awaited<ReturnType<typeof api.listWorkspaceTasks>> = [];
      try {
        tasks = await api.listWorkspaceTasks(activeWorkspaceId);
        if (!cancelled) {
          setTasks(tasks);
        }
      } catch (err) {
        logError("加载工作区任务失败", err, { module: "App", workspace_id: activeWorkspaceId });
      }
      // 工作区任务加载完成后，自动恢复上次活跃任务（仅首次），避免每次进入都需手动点击。
      // 持久化任务已删除时的脏值清理由 taskStore.setTasks 单点负责，表现层不做重复兜底。
      if (!cancelled && !resumeAttempted.current) {
        resumeAttempted.current = true;
        if (persistedId && tasks.some((task) => task.task_id === persistedId)) {
          await openTask(persistedId);
        }
      }
    }
    void loadTasks();
    return () => {
      cancelled = true;
    };
  }, [activeWorkspaceId, setTasks, openTask]);

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-background text-foreground">
      {/* 顶部任务栏 */}
      <TopBar />

      {/* 后端启动失败错误横幅 */}
      <BackendErrorBanner onViewLogs={() => setActiveView("logs")} />

      {/* 三栏主体 */}
      <div className="flex flex-1 overflow-hidden">
        {/* 左侧导航栏：固定宽度 ~240px */}
        <Sidebar activeView={activeView} onOpenLogs={() => setActiveView("logs")} onOpenChat={() => setActiveView("chat")} onNewTask={() => setActiveView("new-task")} />

        {activeView === "logs" ? (
          <LogsPage onBack={() => setActiveView("chat")} />
        ) : activeView === "new-task" ? (
          <NewTaskPage onCreated={() => setActiveView("chat")} />
        ) : (
          <>
            {/* 中央主会话区 + 底部输入区：弹性宽度 */}
            <div className="flex flex-1 flex-col overflow-hidden">
              {/* 消息主区域 */}
              <ChatPanel onPickWorkspace={() => setActiveView("new-task")} />

              {/* 底部输入区 */}
              <InputBar />
            </div>

            {/* 右侧信息面板：固定宽度 ~280px */}
            <RightPanel onOpenLogs={() => setActiveView("logs")} />
          </>
        )}
      </div>
    </div>
  );
}
