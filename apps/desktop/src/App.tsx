/**
 * 根组件：三栏 Grid 布局。
 *
 * 组装：
 * - TopBar（顶部任务栏）
 * - Sidebar（左侧导航栏）
 * - ChatPanel（中央主会话区）
 * - RightPanel（右侧信息面板）
 * - InputBar（底部输入区）
 *
 * 布局结构对齐 `docs/desktop-client-development-plan.md` §4 的 ASCII 布局图。
 *
 * @module App
 */

import { Sidebar } from "@/components/layout/Sidebar";
import { TopBar } from "@/components/layout/TopBar";
import { ChatPanel } from "@/components/layout/ChatPanel";
import { RightPanel } from "@/components/layout/RightPanel";
import { InputBar } from "@/components/layout/InputBar";
import { useBackendBootstrap } from "@/hooks/useBackendBootstrap";

/**
 * App 根组件。
 *
 * 使用 CSS Grid 实现五区域布局：
 * - 顶栏：固定高度
 * - 中间区：弹性高度，分为左固定 / 中弹性 / 右固定
 * - 输入区：固定在中央底部
 *
 * 尺寸约定（对齐开发计划 §4.1）：
 * - 默认窗口 1200×800，最小 960×640
 * - 左侧 ~240px，右侧 ~280px，中央弹性
 */
export default function App() {
  useBackendBootstrap();

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-background text-foreground">
      {/* 顶部任务栏 */}
      <TopBar />

      {/* 三栏主体 */}
      <div className="flex flex-1 overflow-hidden">
        {/* 左侧导航栏：固定宽度 ~240px */}
        <Sidebar />

        {/* 中央主会话区 + 底部输入区：弹性宽度 */}
        <div className="flex flex-1 flex-col overflow-hidden">
          {/* 消息主区域 */}
          <ChatPanel />

          {/* 底部输入区 */}
          <InputBar />
        </div>

        {/* 右侧信息面板：固定宽度 ~280px */}
        <RightPanel />
      </div>
    </div>
  );
}
