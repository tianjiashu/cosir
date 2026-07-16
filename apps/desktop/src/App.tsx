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

import { useState } from "react";
import { Sidebar } from "@/components/layout/Sidebar";
import { TopBar } from "@/components/layout/TopBar";
import { ChatPanel } from "@/components/layout/ChatPanel";
import { RightPanel } from "@/components/layout/RightPanel";
import { InputBar } from "@/components/layout/InputBar";
import { BackendErrorBanner } from "@/components/backend/BackendErrorBanner";
import { useBackendBootstrap } from "@/hooks/useBackendBootstrap";
import { LogsPage } from "@/pages/logs/LogsPage";

/** 工作台主视图。 */
type WorkspaceView = "chat" | "logs";

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

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-background text-foreground">
      {/* 顶部任务栏 */}
      <TopBar />

      {/* 后端启动失败错误横幅 */}
      <BackendErrorBanner onViewLogs={() => setActiveView("logs")} />

      {/* 三栏主体 */}
      <div className="flex flex-1 overflow-hidden">
        {/* 左侧导航栏：固定宽度 ~240px */}
        <Sidebar activeView={activeView} onOpenLogs={() => setActiveView("logs")} onOpenChat={() => setActiveView("chat")} />

        {activeView === "logs" ? (
          <LogsPage onBack={() => setActiveView("chat")} />
        ) : (
          <>
            {/* 中央主会话区 + 底部输入区：弹性宽度 */}
            <div className="flex flex-1 flex-col overflow-hidden">
              {/* 消息主区域 */}
              <ChatPanel />

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
