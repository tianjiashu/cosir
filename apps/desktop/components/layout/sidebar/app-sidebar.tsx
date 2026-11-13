import { useSyncExternalStore, type ReactNode } from "react";
import {
  FolderPlusIcon,
  Loader2Icon,
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
import { forkDescendantIds } from "@/components/session-tree/session-tree-model";
import { RowActionsMenu } from "@/components/session-tree/row-actions-menu";
import { WorkspaceTree } from "@/components/session-tree/workspace-tree";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/state-panel";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import type { WorkspaceSession } from "@/lib/api/workspaces";
import { frontendLog } from "@/lib/logging/frontend-log";
import { readPinnedSessionIds, setSessionPinned, subscribePinnedSessionIds } from "@/lib/pinned-session-storage";
import { withShortcut, type AppShortcutId } from "@/lib/keyboard/shortcuts";

export type SidebarSection = "conversation" | "settings" | "workspace-settings" | "observer";

type AppSidebarProps = {
  catalog: WorkspaceCatalog;
  focusedSessionId: number | null;
  section: SidebarSection;
  /** 开始新会话；带 `workspaceId` 时在该工作区开始（侧栏工作区行的「+」）。 */
  onNewConversation: (workspaceId?: number) => void;
  onSelectSession: (session: WorkspaceSession, workspaceId: number) => void;
  onOpenSettings: () => void;
  onOpenWorkspaceSettings: (workspaceId: number) => void;
  onOpenObserver: () => void;
  /** 区头的搜索：打开命令面板（它按会话标题与工作区名搜索），侧栏不另做一套搜索。 */
  onOpenSearch: () => void;
  /** 区头的添加工作区（`useWorkspaceActions.addWorkspaceFromFolder`）。 */
  onAddWorkspace: () => void;
  addingWorkspace: boolean;
  onRequestDelete: (target: DeleteTarget) => void;
  /** 头部一行的控件（侧栏开关），由 AppLayout 提供；放在这一行拖动区里，左侧让出红绿灯。 */
  headerControls: ReactNode;
};

/**
 * 应用侧栏：新会话、「工作区」区头（搜索、添加工作区）、工作区 / 会话树、底部的系统配置与上下文观测入口。
 *
 * 铺在窗口底色（canvas）上，不画边线：与主区的分隔靠两者明度差，底部入口靠 mt-auto 的留白。
 *
 * 只负责呈现与转发用户意图；停靠、隐藏与窄屏抽屉由 AppLayout 根据视口与偏好决定，开关不在侧栏里
 * （见 `SidebarControls`）。
 * 所有文字都不换行并截断——侧栏宽度在动画中变化时，换行会把中文逐字竖排。
 */
export function AppSidebar({
  catalog,
  focusedSessionId,
  section,
  onNewConversation,
  onSelectSession,
  onOpenSettings,
  onOpenWorkspaceSettings,
  onOpenObserver,
  onOpenSearch,
  onAddWorkspace,
  addingWorkspace,
  onRequestDelete,
  headerControls,
}: AppSidebarProps) {
  const { workspaces, status, error, load } = catalog;
  const pinnedSessionIds = useSyncExternalStore(subscribePinnedSessionIds, readPinnedSessionIds, readPinnedSessionIds);

  const togglePinned = (sessionId: number, pinned: boolean) => {
    const saved = setSessionPinned(sessionId, pinned);
    void frontendLog(saved ? "INFO" : "WARNING", "session_pin_changed", saved ? "会话置顶状态已更新" : "会话置顶状态写入失败", {
      data: { sessionId, pinned, saved },
    });
    if (!saved) toast.error(pinned ? "置顶失败" : "取消置顶失败", { description: "本机存储不可用。" });
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden whitespace-nowrap">
      {/* 窗口最上方一行：标题栏拖动区，左侧让出红绿灯，与页面头部的左内边距一致（侧栏开关因此不随侧栏移动）。 */}
      <div className="app-drag-region flex h-14 shrink-0 items-center pl-[max(1rem,var(--titlebar-inset-left))]">{headerControls}</div>

      <div className="shrink-0 px-3 pt-1 pb-3">
        <SidebarLink active={false} icon={<SquarePenIcon className="size-4" />} label="新会话" shortcut="newConversation" onClick={() => onNewConversation()} />
      </div>
      <nav className="flex min-h-0 flex-1 flex-col px-2 pb-4" aria-label="工作区会话列表">
        <div className="flex h-7 shrink-0 items-center gap-0.5 pr-1 pl-2">
          <h2 className="text-muted-foreground min-w-0 flex-1 truncate text-xs">工作区</h2>
          <TooltipIconButton tooltip={withShortcut("搜索会话", "commandPalette")} aria-label="搜索会话" className="text-muted-foreground size-6" onClick={onOpenSearch}>
            <SearchIcon />
          </TooltipIconButton>
          <TooltipIconButton tooltip="添加工作区" aria-label="添加工作区" className="text-muted-foreground size-6" disabled={addingWorkspace} onClick={onAddWorkspace}>
            {addingWorkspace ? <Loader2Icon className="animate-spin" /> : <FolderPlusIcon />}
          </TooltipIconButton>
        </div>
        {/* 目录随推送在后台重读：已有内容时照常显示、不插入加载行（插入会把树往下推，悬停与焦点随之丢失）。 */}
        {status === "loading" && workspaces.length === 0 && <LoadingState size="compact" label="正在加载工作区…" className="px-2" />}
        {status === "error" && <ErrorState className="mx-2 text-xs" message={error} onRetry={() => void load()} />}
        {status === "ready" && workspaces.length === 0 && <EmptyState size="compact" title="暂无工作区" description="点击上方的添加按钮，选择一个本机目录作为工作区。" className="px-2" />}
        {workspaces.length > 0 && (
          <div className="min-h-0 flex-1">
            <WorkspaceTree
              workspaces={workspaces}
              pinnedSessionIds={pinnedSessionIds}
              activeSessionId={section === "conversation" ? focusedSessionId : null}
              selectedWorkspaceId={catalog.selectedWorkspaceId}
              expandedWorkspaceIds={catalog.expandedWorkspaceIds}
              onWorkspaceExpansionChange={catalog.updateWorkspaceExpansion}
              onSelectSession={onSelectSession}
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
                        onSelect: () => onRequestDelete({ kind: "workspace", id: workspace.workspace_id, label: workspace.name, sessionCount: workspace.sessions.length }),
                      },
                    ]}
                  />
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    tabIndex={tabIndex}
                    className="text-muted-foreground"
                    aria-label={`在 ${workspace.name} 中新建会话`}
                    title={`在 ${workspace.name} 中新建会话`}
                    onClick={() => onNewConversation(workspace.workspace_id)}
                  >
                    <PlusIcon className="size-4" />
                  </Button>
                </>
              )}
              renderSessionActions={(item, tabIndex) => (
                <RowActionsMenu
                  label={item.session.full_title}
                  tabIndex={tabIndex}
                  actions={[
                    ...(item.pinnable ? [{
                      label: item.pinned ? "取消置顶" : "置顶",
                      icon: item.pinned ? <PinOffIcon /> : <PinIcon />,
                      onSelect: () => togglePinned(item.session.session_id, !item.pinned),
                    }] : []),
                    {
                      label: "删除",
                      icon: <Trash2Icon />,
                      destructive: true,
                      onSelect: () => onRequestDelete({
                        kind: "session",
                        id: item.session.session_id,
                        label: item.session.full_title,
                        forkCount: forkDescendantIds(item.session).length,
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
