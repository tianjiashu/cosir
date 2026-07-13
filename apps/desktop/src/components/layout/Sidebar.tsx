/**
 * 左侧导航栏（Sidebar）。
 *
 * 组合以下子组件：
 * - ProjectList：项目列表
 * - TaskList：任务列表
 * - HistoryList：历史会话入口（占位）
 * - PluginList：插件/能力入口（占位）
 *
 * 底部保留用户/设置入口占位。
 * 第一版使用 mock 数据，选中态通过内部 state 控制。
 *
 * @module components/layout/Sidebar
 */

import { useState } from "react";
import {
  Settings,
  User,
  Plus,
} from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Button } from "@/components/ui/button";
import { ProjectList, type ProjectItem } from "@/components/sidebar/ProjectList";
import { TaskList, type TaskItem } from "@/components/sidebar/TaskList";
import { HistoryList } from "@/components/sidebar/HistoryList";
import { PluginList } from "@/components/sidebar/PluginList";

/** Mock 项目数据（第一版静态数据）。 */
const MOCK_PROJECTS: ProjectItem[] = [
  { id: "proj-1", name: "coding-agent", path: "~/Documents/coding-agent" },
  { id: "proj-2", name: "deepseek-coding-agent", path: "~/Projects/deepseek" },
  { id: "proj-3", name: "框架 coding agent 能力", path: "~/Projects/framework" },
];

/** Mock 任务数据（第一版静态数据）。 */
const MOCK_TASKS: TaskItem[] = [
  { id: "task-1", title: "调研项目定位", status: "completed" },
  { id: "task-2", title: "写一篇\"比较粗糙\"的文章", status: "running" },
  { id: "task-3", title: "重写博客第二版", status: "pending" },
  { id: "task-4", title: "帮我将 buji-main 部署到 netlify", status: "pending" },
];

/**
 * Sidebar 左侧导航栏组件。
 *
 * 固定宽度 ~240px，通过组合子组件实现各区域功能，
 * 自身只负责整体布局、新建按钮和底部设置区。
 */
export function Sidebar() {
  const [activeProjectId, setActiveProjectId] = useState("proj-1");
  const [activeTaskId, setActiveTaskId] = useState("task-2");

  return (
    <aside className="flex h-full w-60 flex-col border-r border-border bg-sidebar text-sidebar-foreground">
      {/* 应用标题 */}
      <div className="flex h-12 items-center gap-2 px-4 font-semibold tracking-tight">
        <span className="text-lg">⚡</span>
        <span>Coding Agent</span>
      </div>

      <Separator />

      {/* 主内容区域：可滚动 */}
      <ScrollArea className="flex-1 scrollbar-thin">
        {/* 新建任务按钮 */}
        <div className="p-2">
          <Button variant="outline" className="w-full justify-start gap-2 text-sm">
            <Plus className="h-4 w-4" />
            新建任务
          </Button>
        </div>

        {/* 项目列表 — 使用独立子组件 */}
        <ProjectList
          projects={MOCK_PROJECTS}
          activeId={activeProjectId}
          onSelect={setActiveProjectId}
        />

        {/* 任务列表 — 使用独立子组件 */}
        <TaskList
          tasks={MOCK_TASKS}
          activeId={activeTaskId}
          onSelect={setActiveTaskId}
        />

        <Separator className="my-2" />

        {/* 历史会话入口 — 使用独立子组件 */}
        <HistoryList />

        {/* 插件入口 — 使用独立子组件 */}
        <PluginList />
      </ScrollArea>

      <Separator />

      {/* 底部用户 / 设置入口（占位） */}
      <div className="flex items-center gap-2 p-2">
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-muted text-xs font-medium">
          <User className="h-4 w-4" />
        </div>
        <span className="flex-1 truncate text-sm">Lucky/Dog</span>
        <Button variant="ghost" size="icon" className="h-7 w-7">
          <Settings className="h-4 w-4" />
        </Button>
      </div>
    </aside>
  );
}
