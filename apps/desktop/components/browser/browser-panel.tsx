import { BotIcon, GlobeIcon, Maximize2Icon, MessageSquareWarningIcon, Minimize2Icon, PlusIcon, XIcon } from "lucide-react";
import { useState, type FormEvent } from "react";

import { AgentActivityBar } from "@/components/browser/agent-activity-bar";
import { BrowserView } from "@/components/browser/browser-view";
import { BrowserDialogBanner } from "@/components/browser/dialog-banner";
import { DownloadBar } from "@/components/browser/download-bar";
import { WebPageIcon } from "@/components/links/link-target-icon";
import { DockFrame, type DockSizing } from "@/components/layout/dock-frame";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { EmptyState } from "@/components/ui/state-panel";
import { panelTabClass } from "@/components/ui/panel-tokens";
import { openUserTab } from "@/lib/browser/open-tab";
import { displayedTab, useBrowserPanelStore } from "@/lib/browser/panel-store";
import { useSessionBrowserDownloads, useSessionBrowserTabs } from "@/lib/browser/host-state-store";
import { desktopHost } from "@/lib/desktop-host";
import { tabLabel } from "@/lib/browser/browser-tab-label";
import { useSessionExecutionStatus } from "@/lib/session-status/session-status-store";
import { notify } from "@/lib/notify";
import { cn } from "@/lib/utils";

/**
 * 浏览器面板：一个 Session 的标签页（Agent 与用户开的都在这里），独立的停靠面板，不与工作台混排。
 *
 * 标签条只列这个 Session 的标签（宿主事实，经 `host-state-store` 镜像）；显示哪个标签由 `panel-store` 决定（选中项、
 * 弹窗开出的标签跟随、关闭后回到来源或最新的标签，规则都在那里）。`+` 与空状态里的地址栏都经 `openUserTab`
 * 在这个 Session 里新开标签。关闭标签就是关掉宿主里的标签页（Agent 也就看不到了），与在交互终端里关闭会话一致。
 *
 * 待答的网页弹窗在标签条上有标记、选中时在网页上方显示提示条（都在原生画面占位区之外）；没有弹窗时，Agent 在
 * 运行或正在操作这个标签就显示一行提示（`AgentActivityBar`）。这个 Session 最近的下载显示在面板底部：下载归
 * Session 不归标签（弹出页下载后常自己关掉），所以与显示哪个标签、有没有标签都无关。
 */
export function BrowserPanel({ sessionId, sizing, maximized, onToggleMaximized, onClose }: {
  sessionId: number | null;
  sizing: DockSizing;
  maximized: boolean;
  onToggleMaximized: () => void;
  onClose: () => void;
}) {
  const tabs = useSessionBrowserTabs(sessionId);
  const downloads = useSessionBrowserDownloads(sessionId);
  const selectedId = useBrowserPanelStore((state) => (sessionId === null ? undefined : state.selected[sessionId]));
  const select = useBrowserPanelStore((state) => state.select);
  const active = displayedTab(tabs, selectedId);
  const host = desktopHost;
  const canOpen = host !== null && sessionId !== null;
  const agentRunning = useSessionExecutionStatus(sessionId) === "running";

  const openTab = (url?: string) => {
    if (!canOpen) return;
    openUserTab(sessionId, url).catch((error: unknown) => notify.error("无法新开标签页", { error }));
  };
  // 有标签才有关闭按钮，而标签只来自宿主镜像、按 Session 筛选：走到这里宿主与 Session 必然在。
  const closeTab = (tabId: string) => {
    host!.browser.close(tabId).catch((error: unknown) => notify.error("关闭标签页失败", { error }));
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
              const title = tabLabel(tab.title, tab.url);
              const selected = tab.tabId === active?.tabId;
              return (
                <div key={tab.tabId} className={panelTabClass(selected, "max-w-48 shrink")}>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={selected}
                    className="flex min-w-0 flex-1 items-center gap-1.5 self-stretch text-left outline-none"
                    title={`${title}${tab.createdBy === "agent" ? " · Agent 打开" : ""}`}
                    onClick={() => select(tab.sessionId, tab.tabId)}
                  >
                    {tab.dialog
                      ? <MessageSquareWarningIcon className="text-info size-3.5 shrink-0" aria-label="网页弹窗待处理" />
                      : tab.agentBusy
                        ? <BotIcon className="text-info size-3.5 shrink-0 animate-pulse" aria-label="Agent 正在操作" />
                        : tab.loading
                        ? <Spinner className="size-3.5 shrink-0" />
                        : <WebPageIcon url={tab.url} className="size-3.5" />}
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
      {/* 原生网页不受 DOM 圆角裁切；内容底边退入面板圆角范围，实时画面与冻结帧共用原来的矩形槽。 */}
      <div className="flex min-h-0 flex-1 flex-col pb-(--radius-xl)">
        {!host ? (
          <EmptyState className="m-4" title="内置浏览器只在桌面应用中可用" />
        ) : sessionId === null ? (
          <EmptyState className="m-4" icon={<GlobeIcon />} title="打开一个会话后使用浏览器" description="标签页归属于对话：Agent 与你在同一个会话里看到同一组标签。" />
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
            {active.operation === "unknown" && <p role="status" className="px-3 py-2 text-sm text-warning">页面操作尚未确认结束，暂不能继续自动操作。请先检查页面，必要时关闭重开；不要重复提交。</p>}
            {!active.dialog && active.operation !== "unknown" && (agentRunning || active.agentBusy) && <AgentActivityBar busy={active.agentBusy} />}
            <BrowserView key={active.tabId} host={host} tabId={active.tabId} url={active.url} loading={active.loading} crashed={active.crashed} />
          </>
        ) : (
          <NewTabPrompt onOpen={openTab} />
        )}
        {host && sessionId !== null && downloads.length > 0 && <DownloadBar key={sessionId} host={host} downloads={downloads} />}
      </div>
    </DockFrame>
  );
}

/** 这个会话还没有标签页：输入网址直接开一个。 */
function NewTabPrompt({ onOpen }: { onOpen: (url: string) => void }) {
  const [address, setAddress] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (address.trim()) onOpen(address.trim());
  };
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <form className="flex w-full max-w-md items-center gap-2" onSubmit={submit}>
        <Input aria-label="网址" className={cn("h-9")} placeholder="输入网址" value={address} spellCheck={false} onChange={(event) => setAddress(event.target.value)} />
      </form>
    </div>
  );
}
