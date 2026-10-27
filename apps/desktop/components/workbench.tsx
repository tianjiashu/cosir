"use client";

import { Tabs } from "radix-ui";
import { useStore } from "zustand";
import { isWorkbenchDraftDirty } from "@/lib/workbench/drafts";
import type { WorkbenchAgentTeamConfigurationDraftTab } from "@/lib/workbench/types";

import {
  FolderTreeIcon,
  XIcon,
  PanelRightCloseIcon,
  BotIcon,
  UserCogIcon,
  NetworkIcon,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useEffect } from "react";
import { PanelDock } from "@/components/ui/panel-dock";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { WorkbenchAgentStatusProvider } from "@/lib/workbench/agent-status";
import { WorkbenchAgentTabStatus } from "@/components/workbench-agent-tab-status";
import { WorkbenchSurface } from "@/components/workbench-surface";
import { useWorkbenchStore } from "@/lib/workbench/store";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { WorkbenchTab } from "@/lib/workbench/types";

/** 标签图标按 tab 种类区分：子 Agent 运行与两类配置草稿一眼可辨。新增 kind 时此表强制补齐。 */
const TAB_KIND_ICONS: Record<WorkbenchTab["kind"], LucideIcon> = {
  files: FolderTreeIcon,
  agent: BotIcon,
  "agent-configuration-draft": UserCogIcon,
  "agent-team-configuration-draft": NetworkIcon,
};

/** 工具卡片的已保存标记是会话基线的投影，不再参与草稿状态判定。 */
function TeamSessionIndicator({
  tab,
}: {
  tab: WorkbenchAgentTeamConfigurationDraftTab;
}) {
  const saved = useStore(
    tab.session.store,
    (state) => state.baseline !== null && !state.dirty,
  );
  const setIndicator = useWorkbenchStore(
    (state) => state.setTeamDraftSavedIndicator,
  );
  useEffect(() => {
    setIndicator(tab.id, saved);
  }, [setIndicator, tab.id, saved]);
  return null;
}

/** Team 草稿直接订阅编辑会话，未激活标签也能及时显示未保存状态。 */
function TeamDraftLabel({ tab, expanded = false }: { tab: WorkbenchAgentTeamConfigurationDraftTab; expanded?: boolean; }) {
  const dirty = useStore(tab.session.store, state => state.dirty);
  return <DraftLabel title={tab.title} dirty={dirty} expanded={expanded} />;
}

function DraftLabel({ title, dirty, expanded = false }: { title: string; dirty: boolean; expanded?: boolean; }) {
  return <>
    <span className="truncate">{title}</span>
    {dirty && (expanded ? <span> · 未保存</span> : <><span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-current" /><span className="sr-only">未保存</span></>)}
  </>;
}

export function Workbench({
  filesVisible = true,
  workspaceId,
  onRequestClose,
  width, minWidth, maxWidth, onWidthChange,
}: {
  filesVisible?: boolean;
  width: number;
  minWidth: number;
  maxWidth: number;
  onWidthChange: (width: number) => void;
  workspaceId: number | null;
  onRequestClose: () => void;
}) {
  const sessionWorkspaceId = useWorkbenchStore(state => state.workspaceId);
  const allTabs = useWorkbenchStore((state) => state.tabs);
  const tabs = allTabs.filter(tab => filesVisible || tab.kind !== "files");
  const activeTabId = useWorkbenchStore((state) => state.activeTabId);
  const panelOpen = useWorkbenchStore((state) => state.panelOpen);
  const activateTab = useWorkbenchStore((state) => state.activateTab);
  const closeTab = useWorkbenchStore((state) => state.closeTab);
  const setPanelOpen = useWorkbenchStore((state) => state.setPanelOpen);

  const closeTabNow = useCallback(
    (tabId: string) => {
      void frontendLog("INFO", "workbench_tab_closed", "Workbench 标签页关闭", {
        data: { tabId },
      });
      closeTab(tabId);
    },
    [closeTab],
  );

  /**
   * 标签栏上的关闭：会连同 store 中的草稿一起丢弃（含未激活标签），未保存时先确认。
   * 草稿 surface 自带的关闭按钮已自行确认，走 closeTabNow，避免重复询问。
   */
  const requestCloseTab = useCallback(
    async (tabId: string) => {
      const tab = tabs.find((candidate) => candidate.id === tabId);
      if (tab && isWorkbenchDraftDirty(tab)) {
        const discard = await requestConfirmation({
          title: "关闭配置草稿？",
          description: `「${tab.title}」尚未保存，关闭后修改会丢失。`,
          confirmLabel: "放弃并关闭",
          tone: "destructive",
          icon: "discard",
        });
        if (!discard) return;
      }
      closeTabNow(tabId);
    },
    [closeTabNow, tabs],
  );

  useEffect(() => {
    if (workspaceId === null && panelOpen) setPanelOpen(false);
  }, [panelOpen, setPanelOpen, workspaceId]);

  // 工作区归属先随路由提交，store 随 Provider effect 清理；间隙不呈现旧工作区内容。
  if (sessionWorkspaceId !== workspaceId) return null;

  const indicators = tabs.map((tab) =>
    tab.kind === "agent-team-configuration-draft" ? (
      <TeamSessionIndicator key={tab.id} tab={tab} />
    ) : null,
  );
  if (workspaceId === null || !panelOpen || tabs.length === 0)
    return <>{indicators}</>;
  const activeTab = tabs.find((tab) => tab.id === activeTabId) ?? tabs[0];
  return (
    <>
      {indicators}
      <WorkbenchAgentStatusProvider workspaceId={workspaceId} tabs={tabs}>
      <PanelDock side="right" size={width} minSize={minWidth} maxSize={maxWidth}
        onSizeChange={onWidthChange} resizeLabel="调整工作台宽度">
      <Tabs.Root
        value={activeTab.id}
        onValueChange={(value) => activateTab(String(value))}
        role="complementary"
        className="dock-panel bg-background flex min-h-0 flex-col"
        style={{ width }}
        aria-label="工作台"
      >
        <div className="flex h-11 shrink-0 items-center gap-1 border-b px-2">
          <Tabs.List
            className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
            aria-label="工作台标签页"
          >
            {tabs.map((tab) => {
              const TabIcon = TAB_KIND_ICONS[tab.kind];
              return (
                <div
                  key={tab.id}
                  className={cn("group flex h-8 max-w-56 min-w-0 items-center gap-1 rounded-md pr-1 pl-2 text-xs", tab.id === activeTab.id ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground")}
                >
                  <Tooltip disableHoverablePopup>
                    <TooltipTrigger render={<Tabs.Trigger
                      value={tab.id}
                      className="flex min-w-0 flex-1 items-center gap-1.5 self-stretch text-left focus-visible:outline-2 focus-visible:outline-ring"
                    />} >
                      {tab.kind === "agent" ? <WorkbenchAgentTabStatus taskId={tab.taskId} /> : <TabIcon className="size-3.5 shrink-0" aria-hidden="true" />}
                      {tab.kind === "agent-team-configuration-draft"
                        ? <TeamDraftLabel tab={tab} />
                        : <DraftLabel title={tab.title} dirty={tab.kind === "agent-configuration-draft" && tab.dirty} />}
                    </TooltipTrigger>
                    <TooltipContent side="top">{tab.kind === "agent-team-configuration-draft" ? <TeamDraftLabel tab={tab} expanded /> : <DraftLabel title={tab.title} dirty={tab.kind === "agent-configuration-draft" && tab.dirty} expanded />}{tab.kind === "agent" && <WorkbenchAgentTabStatus taskId={tab.taskId} expanded />}</TooltipContent>
                  </Tooltip>
                  <button
                    type="button"
                    className="flex size-6 shrink-0 items-center justify-center rounded opacity-60 hover:bg-background/80 hover:opacity-100"
                    aria-label={`关闭 ${tab.title}`}
                    onClick={() => void requestCloseTab(tab.id)}
                  >
                    <XIcon className="size-3" aria-hidden="true" />
                  </button>
                </div>
              );
            })}
          </Tabs.List>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="收起工作台"
            onClick={onRequestClose}
          >
            <PanelRightCloseIcon />
          </Button>
        </div>
        {tabs.map((tab) => (
          <Tabs.Content
            key={tab.id}
            value={tab.id}
            className="min-h-0 flex-1 overflow-hidden"
          >
            {tab.id === activeTab.id && (
              <WorkbenchSurface tab={tab} onClose={() => closeTabNow(tab.id)} />
            )}
          </Tabs.Content>
        ))}
      </Tabs.Root>
      </PanelDock>
      </WorkbenchAgentStatusProvider>
    </>
  );
}
