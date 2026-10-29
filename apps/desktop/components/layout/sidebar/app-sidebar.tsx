"use client";

import { useSyncExternalStore, type ReactNode } from "react";
import {
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  SearchIcon,
  Settings2Icon,
  SquarePenIcon,
} from "lucide-react";
import { toast } from "sonner";

import type { WorkspaceCatalog } from "@/components/layout/use-workspace-catalog";
import type { DeleteTarget } from "@/components/layout/use-workspace-actions";
import { ResourceDeleteButton } from "@/components/resource-delete-button";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { TaskActionsMenu } from "@/components/task-tree/task-actions-menu";
import { WorkspaceTree } from "@/components/task-tree/workspace-tree";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import type { WorkspaceTask } from "@/lib/api/workspaces";
import { frontendLog } from "@/lib/logging/frontend-log";
import { readPinnedTaskIds, setTaskPinned, subscribePinnedTaskIds } from "@/lib/pinned-task-storage";
import { withShortcut, type AppShortcutId } from "@/lib/shortcuts";
import { cn } from "@/lib/utils";

export type SidebarSection = "conversation" | "settings" | "workspace-settings" | "observer";

type AppSidebarProps = {
  catalog: WorkspaceCatalog;
  focusedTaskId: number | null;
  section: SidebarSection;
  /** rail：仅图标；expanded：完整侧栏（宽屏停靠或窄屏抽屉）。 */
  variant: "rail" | "expanded";
  onToggle: () => void;
  onNewConversation: () => void;
  onSelectTask: (task: WorkspaceTask, workspaceId: number) => void;
  onOpenSettings: () => void;
  onOpenWorkspaceSettings: (workspaceId: number) => void;
  onOpenObserver: () => void;
  onRequestDelete: (target: DeleteTarget) => void;
};

/** 侧栏开关：展开时在侧栏顶部，图标栏时由 AppLayout 放进页面头部。 */
export function SidebarToggleButton({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const label = collapsed ? "展开侧栏" : "收起侧栏";
  return (
    <TooltipIconButton tooltip={withShortcut(label, "toggleSidebar")} className="size-7" onClick={onToggle} aria-label={label}>
      {collapsed ? <PanelLeftOpenIcon /> : <PanelLeftCloseIcon />}
    </TooltipIconButton>
  );
}

/**
 * 应用侧栏：新对话、工作区 / 任务树、底部的系统配置与上下文观测入口。
 *
 * 铺在窗口底色（canvas）上，不画边线：与主区的分隔靠两者明度差，底部入口靠 mt-auto 的留白。
 *
 * 只负责呈现与转发用户意图；停靠 / 抽屉 / 图标栏的切换由 AppLayout 根据视口与偏好决定。
 * 所有文字都不换行并截断——侧栏宽度在动画中变化时，换行会把中文逐字竖排。
 */
export function AppSidebar({
  catalog,
  focusedTaskId,
  section,
  variant,
  onToggle,
  onNewConversation,
  onSelectTask,
  onOpenSettings,
  onOpenWorkspaceSettings,
  onOpenObserver,
  onRequestDelete,
}: AppSidebarProps) {
  const rail = variant === "rail";
  const { workspaces, status, error, load } = catalog;
  const pinnedTaskIds = useSyncExternalStore(subscribePinnedTaskIds, readPinnedTaskIds, readPinnedTaskIds);

  const togglePinned = (taskId: number, pinned: boolean) => {
    const saved = setTaskPinned(taskId, pinned);
    void frontendLog(saved ? "INFO" : "WARNING", "task_pin_changed", saved ? "任务置顶状态已更新" : "任务置顶状态写入失败", {
      data: { taskId, pinned, saved },
    });
    if (!saved) toast.error(pinned ? "置顶失败" : "取消置顶失败", { description: "本机存储不可用。" });
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden whitespace-nowrap">
      {/*
        窗口最上方一行，同时是标题栏拖动区。图标栏只有 56px，放不下 macOS 红绿灯，
        这一行在图标栏下留空，展开开关改由 AppLayout 放进页面头部（紧跟红绿灯）。
      */}
      <div data-tauri-drag-region="deep" className={cn("flex h-14 shrink-0 items-center justify-between pr-3", !rail && "pl-[max(0.75rem,var(--titlebar-inset-left))]")}>
        {!rail && (
          <>
            <span className="truncate text-sm font-semibold">工作区</span>
            <SidebarToggleButton collapsed={false} onToggle={onToggle} />
          </>
        )}
      </div>

      <div className={cn("shrink-0", rail ? "flex justify-center p-2" : "px-3 pt-3 pb-5")}>
        <SidebarLink rail={rail} active={false} icon={<SquarePenIcon className="size-4" />} label="新对话" shortcut="newConversation" onClick={onNewConversation} />
      </div>
      {!rail && (
        <nav className="flex min-h-0 flex-1 flex-col px-2 pb-4" aria-label="工作区任务列表">
          {status === "loading" && <LoadingState size="compact" label="正在加载工作区…" className="px-2" />}
          {status === "error" && <ErrorState className="mx-2 text-xs" message={error} onRetry={() => void load()} />}
          {status === "ready" && workspaces.length === 0 && <EmptyState size="compact" title="暂无工作区" description="在新对话页添加一个本机目录作为工作区。" className="px-2" />}
          {workspaces.length > 0 && (
            <div className="min-h-0 flex-1">
              <WorkspaceTree
                workspaces={workspaces}
                pinnedTaskIds={pinnedTaskIds}
                activeTaskId={section === "conversation" ? focusedTaskId : null}
                selectedWorkspaceId={catalog.selectedWorkspaceId}
                expandedWorkspaceIds={catalog.expandedWorkspaceIds}
                onWorkspaceExpansionChange={catalog.updateWorkspaceExpansion}
                onSelectWorkspace={catalog.selectWorkspace}
                onOpenWorkspaceSettings={onOpenWorkspaceSettings}
                onSelectTask={onSelectTask}
                renderWorkspaceActions={(workspace, tabIndex) => (
                  <ResourceDeleteButton
                    tabIndex={tabIndex}
                    label={workspace.name}
                    onDelete={() => onRequestDelete({ kind: "workspace", id: workspace.workspace_id, label: workspace.name, taskCount: workspace.tasks.length })}
                  />
                )}
                renderTaskActions={(item, tabIndex) => (
                  <TaskActionsMenu
                    item={item}
                    tabIndex={tabIndex}
                    onTogglePinned={() => togglePinned(item.task.task_id, !item.pinned)}
                    onDelete={() => onRequestDelete({ kind: "task", id: item.task.task_id, label: item.task.full_title })}
                  />
                )}
              />
            </div>
          )}
        </nav>
      )}

      <div className={cn("mt-auto flex shrink-0 flex-col gap-1", rail ? "items-center p-2" : "p-3")}>
        <SidebarLink rail={rail} active={section === "settings"} icon={<Settings2Icon className="size-4" />} label="系统配置" shortcut="openSettings" onClick={onOpenSettings} />
        <SidebarLink rail={rail} active={section === "observer"} icon={<SearchIcon className="size-4" />} label="上下文观测" onClick={onOpenObserver} />
      </div>
    </div>
  );
}

function SidebarLink({ rail, active, icon, label, shortcut, onClick }: {
  rail: boolean;
  active: boolean;
  icon: ReactNode;
  label: string;
  shortcut?: AppShortcutId;
  onClick: () => void;
}) {
  const title = shortcut ? withShortcut(label, shortcut) : label;
  return (
    <Button
      variant={active ? "secondary" : "ghost"}
      size={rail ? "icon" : "default"}
      className={cn(!rail && "w-full justify-start gap-2")}
      onClick={onClick}
      aria-label={label}
      aria-current={active ? "page" : undefined}
      title={rail ? title : shortcut ? title : undefined}
    >
      {icon}
      {!rail && <span className="truncate">{label}</span>}
    </Button>
  );
}
