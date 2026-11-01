import { useSyncExternalStore, type ReactNode } from "react";
import {
  PinIcon,
  PinOffIcon,
  PlusIcon,
  SearchIcon,
  Settings2Icon,
  SquarePenIcon,
  Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";

import type { WorkspaceCatalog } from "@/components/layout/use-workspace-catalog";
import type { DeleteTarget } from "@/components/layout/use-workspace-actions";
import { forkDescendantIds } from "@/components/task-tree/task-tree-model";
import { RowActionsMenu } from "@/components/task-tree/row-actions-menu";
import { WorkspaceTree } from "@/components/task-tree/workspace-tree";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import type { WorkspaceTask } from "@/lib/api/workspaces";
import { frontendLog } from "@/lib/logging/frontend-log";
import { readPinnedTaskIds, setTaskPinned, subscribePinnedTaskIds } from "@/lib/pinned-task-storage";
import { withShortcut, type AppShortcutId } from "@/lib/keyboard/shortcuts";

export type SidebarSection = "conversation" | "settings" | "workspace-settings" | "observer";

type AppSidebarProps = {
  catalog: WorkspaceCatalog;
  focusedTaskId: number | null;
  section: SidebarSection;
  /** 开始新对话；带 `workspaceId` 时在该工作区开始（侧栏工作区行的「+」）。 */
  onNewConversation: (workspaceId?: number) => void;
  onSelectTask: (task: WorkspaceTask, workspaceId: number) => void;
  onOpenSettings: () => void;
  onOpenWorkspaceSettings: (workspaceId: number) => void;
  onOpenObserver: () => void;
  onRequestDelete: (target: DeleteTarget) => void;
  /** 头部一行的控件（侧栏开关），由 AppLayout 提供；放在这一行拖动区里，左侧让出红绿灯。 */
  headerControls: ReactNode;
};

/**
 * 应用侧栏：新对话、工作区 / 任务树、底部的系统配置与上下文观测入口。
 *
 * 铺在窗口底色（canvas）上，不画边线：与主区的分隔靠两者明度差，底部入口靠 mt-auto 的留白。
 *
 * 只负责呈现与转发用户意图；停靠、隐藏与窄屏抽屉由 AppLayout 根据视口与偏好决定，开关不在侧栏里
 * （见 `SidebarControls`）。
 * 所有文字都不换行并截断——侧栏宽度在动画中变化时，换行会把中文逐字竖排。
 */
export function AppSidebar({
  catalog,
  focusedTaskId,
  section,
  onNewConversation,
  onSelectTask,
  onOpenSettings,
  onOpenWorkspaceSettings,
  onOpenObserver,
  onRequestDelete,
  headerControls,
}: AppSidebarProps) {
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
      {/* 窗口最上方一行：标题栏拖动区，左侧让出红绿灯，与页面头部的左内边距一致（侧栏开关因此不随侧栏移动）。 */}
      <div className="app-drag-region flex h-14 shrink-0 items-center pl-[max(1rem,var(--titlebar-inset-left))]">{headerControls}</div>

      <div className="shrink-0 px-3 pt-3 pb-5">
        <SidebarLink active={false} icon={<SquarePenIcon className="size-4" />} label="新对话" shortcut="newConversation" onClick={() => onNewConversation()} />
      </div>
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
              onSelectTask={onSelectTask}
              renderWorkspaceActions={(workspace, tabIndex) => (
                <>
                  <RowActionsMenu
                    label={`工作区 ${workspace.name}`}
                    tabIndex={tabIndex}
                    actions={[
                      { label: "工作区配置", icon: <Settings2Icon />, onSelect: () => onOpenWorkspaceSettings(workspace.workspace_id) },
                      {
                        label: "删除工作区",
                        icon: <Trash2Icon />,
                        destructive: true,
                        onSelect: () => onRequestDelete({ kind: "workspace", id: workspace.workspace_id, label: workspace.name, taskCount: workspace.tasks.length }),
                      },
                    ]}
                  />
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    tabIndex={tabIndex}
                    className="text-muted-foreground"
                    aria-label={`在 ${workspace.name} 中新建对话`}
                    title={`在 ${workspace.name} 中新建对话`}
                    onClick={() => onNewConversation(workspace.workspace_id)}
                  >
                    <PlusIcon className="size-4" />
                  </Button>
                </>
              )}
              renderTaskActions={(item, tabIndex) => (
                <RowActionsMenu
                  label={item.task.full_title}
                  tabIndex={tabIndex}
                  actions={[
                    ...(item.pinnable ? [{
                      label: item.pinned ? "取消置顶" : "置顶",
                      icon: item.pinned ? <PinOffIcon /> : <PinIcon />,
                      onSelect: () => togglePinned(item.task.task_id, !item.pinned),
                    }] : []),
                    {
                      label: "删除",
                      icon: <Trash2Icon />,
                      destructive: true,
                      onSelect: () => onRequestDelete({
                        kind: "task",
                        id: item.task.task_id,
                        label: item.task.full_title,
                        forkCount: forkDescendantIds(item.task).length,
                      }),
                    },
                  ]}
                />
              )}
            />
          </div>
        )}
        </nav>

      <div className="mt-auto flex shrink-0 flex-col gap-1 p-3">
        <SidebarLink active={section === "settings"} icon={<Settings2Icon className="size-4" />} label="系统配置" shortcut="openSettings" onClick={onOpenSettings} />
        <SidebarLink active={section === "observer"} icon={<SearchIcon className="size-4" />} label="上下文观测" onClick={onOpenObserver} />
      </div>
    </div>
  );
}

function SidebarLink({ active, icon, label, shortcut, onClick }: {
  active: boolean;
  icon: ReactNode;
  label: string;
  shortcut?: AppShortcutId;
  onClick: () => void;
}) {
  return (
    <Button
      variant={active ? "secondary" : "ghost"}
      className="w-full justify-start gap-2"
      onClick={onClick}
      aria-label={label}
      aria-current={active ? "page" : undefined}
      title={shortcut ? withShortcut(label, shortcut) : undefined}
    >
      {icon}
      <span className="truncate">{label}</span>
    </Button>
  );
}
