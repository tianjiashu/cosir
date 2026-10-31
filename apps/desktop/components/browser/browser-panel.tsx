import { GlobeIcon, HandIcon, Maximize2Icon, Minimize2Icon, PlusIcon, XIcon } from "lucide-react";
import { useState, type FormEvent } from "react";

import { BrowserView } from "@/components/browser/browser-view";
import { HandoffBanner } from "@/components/handoff/handoff-banner";
import { DockFrame } from "@/components/layout/dock-frame";
import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { EmptyState } from "@/components/ui/state-panel";
import { panelTabClass } from "@/components/ui/panel-tokens";
import { openUserTab } from "@/lib/browser/open-tab";
import { useBrowserPanelStore } from "@/lib/browser/panel-store";
import { useTaskBrowserTabs } from "@/lib/browser/tabs-store";
import { desktopHost } from "@/lib/desktop-host";
import { useHandoffStore } from "@/lib/handoff/store";
import { useHandBack } from "@/lib/handoff/use-hand-back";
import { notify } from "@/lib/notify";
import { cn } from "@/lib/utils";

/**
 * 浏览器面板：一个 Task 的标签页（Agent 与用户开的都在这里），独立的停靠面板，不与工作台混排。
 *
 * 标签条只列这个 Task 的标签（宿主事实，经 `tabs-store` 镜像）；选中项存在 `panel-store`，选中的标签关闭后
 * 回落到最新的标签。`+` 与空状态里的地址栏都经 `openUserTab` 在这个 Task 里新开标签。关闭标签就是关掉
 * 宿主里的标签页（Agent 也就看不到了），与在交互终端里关闭会话一致。
 *
 * 交接中的标签带一个手形标记，选中时内容上方显示交接提示。关闭交接中的标签即交还：宿主无法把「标签关了」
 * 告诉后端，用户在这里关掉它是唯一能及时唤醒 Agent 的时机；重放时后端报告标签已不在。
 */
export function BrowserPanel({ taskId, width, minWidth, maxWidth, onWidthChange, maximized, onToggleMaximized, onClose }: {
  taskId: number | null;
  width: number;
  minWidth: number;
  maxWidth: number;
  onWidthChange: (width: number) => void;
  maximized: boolean;
  onToggleMaximized: () => void;
  onClose: () => void;
}) {
  const tabs = useTaskBrowserTabs(taskId ?? -1);
  const selectedId = useBrowserPanelStore((state) => (taskId === null ? undefined : state.selected[taskId]));
  const select = useBrowserPanelStore((state) => state.select);
  const active = tabs.find((tab) => tab.tabId === selectedId) ?? tabs.at(-1);
  const host = desktopHost;
  const canOpen = host !== null && taskId !== null;
  const handoffs = useHandoffStore((state) => (taskId === null ? null : state.tasks[taskId]));
  const handoffOf = (tabId: string) => handoffs?.find((entry) => entry.surface === "browser" && entry.resourceId === tabId) ?? null;
  const activeHandoff = active ? handoffOf(active.tabId) : null;
  const { handBack } = useHandBack(taskId ?? -1);

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
      width={width}
      minWidth={minWidth}
      maxWidth={maxWidth}
      onWidthChange={onWidthChange}
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
                    {tab.loading
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
          {activeHandoff && <HandoffBanner taskId={taskId} entry={activeHandoff} />}
          <BrowserView key={active.tabId} host={host} tabId={active.tabId} url={active.url} loading={active.loading} crashed={active.crashed} />
        </>
      ) : (
        <NewTabPrompt onOpen={openTab} />
      )}
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
