import { BotIcon, GlobeIcon, HandIcon, Maximize2Icon, MessageSquareWarningIcon, Minimize2Icon, PlusIcon, XIcon } from "lucide-react";
import { useState, type FormEvent } from "react";

import { AgentActivityBar } from "@/components/browser/agent-activity-bar";
import { BrowserView } from "@/components/browser/browser-view";
import { BrowserDialogBanner } from "@/components/browser/dialog-banner";
import { DownloadBar } from "@/components/browser/download-bar";
import { HandoffBanner } from "@/components/handoff/handoff-banner";
import { DockFrame, type DockSizing } from "@/components/layout/dock-frame";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { EmptyState } from "@/components/ui/state-panel";
import { panelTabClass } from "@/components/ui/panel-tokens";
import { openUserTab } from "@/lib/browser/open-tab";
import { displayedTab, useBrowserPanelStore } from "@/lib/browser/panel-store";
import { useTaskBrowserDownloads, useTaskBrowserTabs } from "@/lib/browser/host-state-store";
import { desktopHost } from "@/lib/desktop-host";
import { useHandoffStore } from "@/lib/handoff/store";
import { useHandBack } from "@/lib/handoff/use-hand-back";
import { useTaskExecutionStatus } from "@/lib/task-status/task-status-store";
import { notify } from "@/lib/notify";
import { cn } from "@/lib/utils";

/**
 * 浏览器面板：一个 Task 的标签页（Agent 与用户开的都在这里），独立的停靠面板，不与工作台混排。
 *
 * 标签条只列这个 Task 的标签（宿主事实，经 `host-state-store` 镜像）；显示哪个标签由 `panel-store` 决定（选中项、
 * 弹窗开出的标签跟随、关闭后回到来源或最新的标签，规则都在那里）。`+` 与空状态里的地址栏都经 `openUserTab`
 * 在这个 Task 里新开标签。关闭标签就是关掉宿主里的标签页（Agent 也就看不到了），与在交互终端里关闭会话一致。
 *
 * 「等你操作」的状态在标签条上各有标记、选中时在网页上方显示提示条（都在原生画面占位区之外）：待答的网页
 * 弹窗优先于交接，因为弹窗不答页面就停着；两者都没有时，Agent 在运行或正在操作这个标签就显示一行提示
 * （`AgentActivityBar`）。关闭交接中的标签即交还：宿主无法把「标签关了」告诉后端，用户在
 * 这里关掉它是唯一能及时唤醒 Agent 的时机；重放时后端报告标签已不在。这个 Task 最近的下载显示在面板底部：下载归
 * Task 不归标签（弹出页下载后常自己关掉），所以与显示哪个标签、有没有标签都无关。
 */
export function BrowserPanel({ taskId, sizing, maximized, onToggleMaximized, onClose }: {
  taskId: number | null;
  sizing: DockSizing;
  maximized: boolean;
  onToggleMaximized: () => void;
  onClose: () => void;
}) {
  const tabs = useTaskBrowserTabs(taskId ?? -1);
  const downloads = useTaskBrowserDownloads(taskId ?? -1);
  const selectedId = useBrowserPanelStore((state) => (taskId === null ? undefined : state.selected[taskId]));
  const select = useBrowserPanelStore((state) => state.select);
  const active = displayedTab(tabs, selectedId);
  const host = desktopHost;
  const canOpen = host !== null && taskId !== null;
  const handoffs = useHandoffStore((state) => (taskId === null ? null : state.tasks[taskId]));
  const handoffOf = (tabId: string) => handoffs?.find((entry) => entry.surface === "browser" && entry.resourceId === tabId) ?? null;
  const activeHandoff = active ? handoffOf(active.tabId) : null;
  const { handBack } = useHandBack(taskId ?? -1);
  const agentRunning = useTaskExecutionStatus(taskId) === "running";

  const openTab = (url?: string) => {
    if (!canOpen) return;
    openUserTab(taskId, url).catch((error: unknown) => notify.error("无法新开标签页", { error }));
  };
  const closeTab = (tabId: string) => {
    const handoff = handoffOf(tabId);
    host?.browser.close(tabId).then(
      () => { if (handoff) void handBack(handoff.toolCallId); },
      (error: unknown) => notify.error("关闭标签页失败", { error }),
    );
  };

  return (
    <DockFrame
      label="浏览器"
      sizing={sizing}
      fill={maximized}
      header={(
        <>
          <div role="tablist" aria-label="浏览器标签页" className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
            {tabs.map((tab) => {
              const title = tab.title || tab.url.replace(/^about:blank$/, "") || "新标签页";
              const selected = tab.tabId === active?.tabId;
              return (
                <div key={tab.tabId} className={panelTabClass(selected, "max-w-48 shrink")}>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={selected}
                    className="flex min-w-0 flex-1 items-center gap-1.5 self-stretch text-left outline-none"
                    title={`${title}${tab.createdBy === "agent" ? " · Agent 打开" : ""}`}
                    onClick={() => select(tab.taskId, tab.tabId)}
                  >
                    {tab.dialog
                      ? <MessageSquareWarningIcon className="text-info size-3.5 shrink-0" aria-label="网页弹窗待处理" />
                      : tab.agentBusy
                        ? <BotIcon className="text-info size-3.5 shrink-0 animate-pulse" aria-label="Agent 正在操作" />
                        : tab.loading
                        ? <Spinner className="size-3.5 shrink-0" />
                        : handoffOf(tab.tabId)
                          ? <HandIcon className="text-info size-3.5 shrink-0" aria-label="交接中" />
                          : <GlobeIcon className="size-3.5 shrink-0" aria-hidden="true" />}
                    <span className="truncate">{title}</span>
                  </button>
                  <TooltipIconButton tooltip="关闭标签页" side="bottom" className="size-6 shrink-0 opacity-60 hover:opacity-100" aria-label={`关闭 ${title}`} onClick={() => closeTab(tab.tabId)}>
                    <XIcon className="size-3" />
                  </TooltipIconButton>
                </div>
              );
            })}
            {canOpen && (
              <TooltipIconButton tooltip="新标签页" className="size-7 shrink-0" aria-label="新标签页" onClick={() => openTab()}>
                <PlusIcon />
              </TooltipIconButton>
            )}
          </div>
          <TooltipIconButton tooltip={maximized ? "还原" : "铺满"} className="size-7" aria-label={maximized ? "还原浏览器面板" : "铺满浏览器面板"} onClick={onToggleMaximized}>
            {maximized ? <Minimize2Icon /> : <Maximize2Icon />}
          </TooltipIconButton>
          <TooltipIconButton tooltip="收起浏览器" className="size-7" aria-label="收起浏览器" onClick={onClose}>
            <XIcon />
          </TooltipIconButton>
        </>
      )}
    >
      {!host ? (
        <EmptyState className="m-4" title="内置浏览器只在桌面应用中可用" />
      ) : taskId === null ? (
        <EmptyState className="m-4" icon={<GlobeIcon />} title="打开一个对话后使用浏览器" description="标签页归属于对话：Agent 与你在同一个对话里看到同一组标签。" />
      ) : active ? (
        <>
          {active.dialog && (
            <BrowserDialogBanner
              key={active.dialog.id}
              host={host}
              tabId={active.tabId}
              dialog={active.dialog}
            />
          )}
          {activeHandoff && <HandoffBanner taskId={taskId} entry={activeHandoff} />}
          {!active.dialog && !activeHandoff && (agentRunning || active.agentBusy) && <AgentActivityBar busy={active.agentBusy} />}
          <BrowserView key={active.tabId} host={host} tabId={active.tabId} url={active.url} loading={active.loading} crashed={active.crashed} />
        </>
      ) : (
        <NewTabPrompt onOpen={openTab} />
      )}
      {host && taskId !== null && downloads.length > 0 && <DownloadBar key={taskId} host={host} downloads={downloads} />}
    </DockFrame>
  );
}

/** 这个对话还没有标签页：输入网址直接开一个。 */
function NewTabPrompt({ onOpen }: { onOpen: (url: string) => void }) {
  const [address, setAddress] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (address.trim()) onOpen(address.trim());
  };
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6">
      <form className="flex w-full max-w-md items-center gap-2" onSubmit={submit}>
        <Input aria-label="网址" className={cn("h-9")} placeholder="输入网址" value={address} spellCheck={false} onChange={(event) => setAddress(event.target.value)} />
      </form>
      <p className="text-muted-foreground text-center text-xs">Agent 在这个对话里打开的网页也会出现在这里。</p>
    </div>
  );
}
