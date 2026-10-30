"use client";

import { Tabs } from "radix-ui";
import {
  FolderTreeIcon,
  XIcon,
  PanelRightCloseIcon,
  BotIcon,
  UserCogIcon,
  NetworkIcon,
  LayoutDashboardIcon,
  type LucideIcon,
} from "lucide-react";
import { useCallback } from "react";

import { PanelDock } from "@/components/ui/panel-dock";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { PANEL_HEADER_CLASS, panelTabClass } from "@/components/ui/panel-tokens";
import { WorkbenchAgentTabStatus } from "@/components/workbench-agent-tab-status";
import { WorkbenchSurface } from "@/components/workbench-surface";
import type { Workspace } from "@/lib/api/workspaces";
import { frontendLog } from "@/lib/logging/frontend-log";
import { WorkbenchAgentStatusProvider } from "@/lib/workbench/agent-status";
import { confirmCloseWorkbenchTab, useWorkbenchTabDirty } from "@/lib/workbench/drafts";
import { useWorkbenchStore } from "@/lib/workbench/store";
import type { WorkbenchTab } from "@/lib/workbench/types";

/** 标签图标按 tab 种类区分：子 Agent 运行与两类配置草稿一眼可辨。新增 kind 时此表强制补齐。 */
const TAB_KIND_ICONS: Record<WorkbenchTab["kind"], LucideIcon> = {
  files: FolderTreeIcon,
  agent: BotIcon,
  "agent-configuration-draft": UserCogIcon,
  "agent-team-configuration-draft": NetworkIcon,
  "task-overview": LayoutDashboardIcon,
};

/** 草稿标签直接订阅会话，未激活标签也能及时显示未保存状态。 */
function TabLabel({ tab, expanded = false }: { tab: WorkbenchTab; expanded?: boolean }) {
  const dirty = useWorkbenchTabDirty(tab);
  return <>
    <span className="truncate">{tab.title}</span>
    {dirty && (expanded ? <span> · 未保存</span> : <><span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-current" /><span className="sr-only">未保存</span></>)}
  </>;
}

/**
 * 右侧工作台：展示一个工作区桶里的标签。是否渲染、显示哪些标签由布局决定（见 AppLayout），
 * 这里只负责标签栏、关闭确认与当前面板；只挂载激活标签的面板。
 */
export function Workbench({
  workspace,
  tabs,
  activeTabId,
  onRequestClose,
  width, minWidth, maxWidth, onWidthChange,
}: {
  workspace: Workspace;
  tabs: WorkbenchTab[];
  activeTabId: string | null;
  width: number;
  minWidth: number;
  maxWidth: number;
  onWidthChange: (width: number) => void;
  onRequestClose: () => void;
}) {
  const workspaceId = workspace.workspace_id;
  const activateTab = useWorkbenchStore((state) => state.activateTab);
  const closeTab = useWorkbenchStore((state) => state.closeTab);

  const closeTabNow = useCallback(
    (tabId: string) => {
      void frontendLog("INFO", "workbench_tab_closed", "Workbench 标签页关闭", {
        data: { workspaceId, tabId },
      });
      closeTab(workspaceId, tabId);
    },
    [closeTab, workspaceId],
  );

  /**
   * 标签栏上的关闭：会连同会话中的草稿一起丢弃（含未激活标签），未保存时先确认。
   * 草稿面板自带的关闭按钮已自行确认，走 closeTabNow，避免重复询问。
   */
  const requestCloseTab = useCallback(
    async (tab: WorkbenchTab) => {
      if (await confirmCloseWorkbenchTab(tab)) closeTabNow(tab.id);
    },
    [closeTabNow],
  );

  const activeTab = tabs.find((tab) => tab.id === activeTabId) ?? tabs[0];
  return (
    <WorkbenchAgentStatusProvider workspaceId={workspaceId} tabs={tabs}>
      <PanelDock side="right" size={width} minSize={minWidth} maxSize={maxWidth}
        onSizeChange={onWidthChange} resizeLabel="调整工作台宽度">
      <Tabs.Root
        value={activeTab.id}
        onValueChange={(value) => activateTab(workspaceId, String(value))}
        role="complementary"
        className="dock-panel bg-background flex min-h-0 flex-col"
        style={{ width }}
        data-workbench-dock
        aria-label="工作台"
      >
        {/* 卡片位于窗口右上角：标签行是标题栏拖动区，并给 Windows 自绘窗口按钮让位（卡片已离窗口右缘 8px）。 */}
        <div data-tauri-drag-region="deep" className={cn(PANEL_HEADER_CLASS, "pr-[max(0.5rem,calc(var(--titlebar-inset-right)-0.5rem))]")}>
          <Tabs.List
            className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
            aria-label="工作台标签页"
          >
            {tabs.map((tab) => {
              const TabIcon = TAB_KIND_ICONS[tab.kind];
              return (
                <div key={tab.id} className={panelTabClass(tab.id === activeTab.id, "max-w-56 shrink")}>
                  <Tooltip disableHoverablePopup>
                    <TooltipTrigger render={<Tabs.Trigger
                      value={tab.id}
                      className="flex min-w-0 flex-1 items-center gap-1.5 self-stretch text-left focus-visible:outline-2 focus-visible:outline-ring"
                    />} >
                      {tab.kind === "agent" ? <WorkbenchAgentTabStatus taskId={tab.taskId} /> : <TabIcon className="size-3.5 shrink-0" aria-hidden="true" />}
                      <TabLabel tab={tab} />
                    </TooltipTrigger>
                    <TooltipContent side="top"><TabLabel tab={tab} expanded />{tab.kind === "agent" && <WorkbenchAgentTabStatus taskId={tab.taskId} expanded />}</TooltipContent>
                  </Tooltip>
                  <TooltipIconButton
                    tooltip="关闭标签"
                    side="top"
                    className="size-6 shrink-0 opacity-60 hover:bg-background/80 hover:opacity-100"
                    aria-label={`关闭 ${tab.title}`}
                    onClick={() => void requestCloseTab(tab)}
                  >
                    <XIcon className="size-3" aria-hidden="true" />
                  </TooltipIconButton>
                </div>
              );
            })}
          </Tabs.List>
          <TooltipIconButton tooltip="收起工作台" className="size-7" aria-label="收起工作台" onClick={onRequestClose}>
            <PanelRightCloseIcon />
          </TooltipIconButton>
        </div>
        {tabs.map((tab) => (
          <Tabs.Content
            key={tab.id}
            value={tab.id}
            className="min-h-0 flex-1 overflow-hidden"
          >
            {tab.id === activeTab.id && (
              <WorkbenchSurface workspace={workspace} tab={tab} onClose={() => closeTabNow(tab.id)} />
            )}
          </Tabs.Content>
        ))}
      </Tabs.Root>
      </PanelDock>
    </WorkbenchAgentStatusProvider>
  );
}
