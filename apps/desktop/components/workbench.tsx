"use client";

import { XIcon, PanelRightCloseIcon, GripVerticalIcon, BotIcon } from "lucide-react";
import { useCallback, useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { WorkbenchSurface } from "@/components/workbench-surface";
import { useWorkbenchStore } from "@/lib/workbench/store";
import { frontendLog } from "@/lib/logging/frontend-log";

export function Workbench({ workspaceId }: { workspaceId: number | null }) {
  const tabs = useWorkbenchStore((state) => state.tabs);
  const activeTabId = useWorkbenchStore((state) => state.activeTabId);
  const panelOpen = useWorkbenchStore((state) => state.panelOpen);
  const panelWidth = useWorkbenchStore((state) => state.panelWidth);
  const activateTab = useWorkbenchStore((state) => state.activateTab);
  const closeTab = useWorkbenchStore((state) => state.closeTab);
  const setPanelOpen = useWorkbenchStore((state) => state.setPanelOpen);
  const setPanelWidth = useWorkbenchStore((state) => state.setPanelWidth);
  const draggingRef = useRef(false);

  const handleCloseTab = useCallback((tabId: string) => {
    void frontendLog("INFO", "workbench_tab_closed", "Workbench 标签页关闭", { data: { tabId } });
    closeTab(tabId);
  }, [closeTab]);

  useEffect(() => {
    if (workspaceId === null && panelOpen) setPanelOpen(false);
  }, [panelOpen, setPanelOpen, workspaceId]);

  const startResize = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    draggingRef.current = true;
    event.currentTarget.setPointerCapture(event.pointerId);
    const move = (moveEvent: PointerEvent) => {
      if (!draggingRef.current) return;
      setPanelWidth(window.innerWidth - moveEvent.clientX);
    };
    const stop = () => {
      draggingRef.current = false;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  }, [setPanelWidth]);

  if (workspaceId === null || !panelOpen || tabs.length === 0) return null;
  const activeTab = tabs.find((tab) => tab.id === activeTabId) ?? tabs[0];
  return (
    <aside className="relative flex min-h-0 shrink-0 flex-col border-l bg-background" style={{ width: panelWidth }} aria-label="Workbench">
      <div
        className="absolute inset-y-0 -left-1 z-10 flex w-2 cursor-col-resize items-center justify-center hover:bg-primary/20"
        role="separator"
        aria-orientation="vertical"
        aria-valuemin={320}
        aria-valuemax={900}
        aria-valuenow={panelWidth}
        tabIndex={0}
        onPointerDown={startResize}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") setPanelWidth(panelWidth + 24);
          if (event.key === "ArrowRight") setPanelWidth(panelWidth - 24);
        }}
      ><GripVerticalIcon className="text-muted-foreground size-3" aria-hidden="true" /></div>
      <div className="flex min-h-12 shrink-0 items-end gap-1 border-b px-2 pt-2">
        <div className="flex min-w-0 flex-1 items-end gap-1 overflow-x-auto" role="tablist" aria-label="Workbench 标签页">
          {tabs.map((tab) => (
            <div
              key={tab.id}
              className={`group flex h-9 max-w-56 min-w-0 items-center gap-1.5 rounded-t-md border border-b-0 px-2 text-xs ${tab.id === activeTab.id ? "bg-background text-foreground" : "bg-muted/40 text-muted-foreground hover:bg-muted"}`}
              aria-selected={tab.id === activeTab.id}
              role="tab"
            >
              <button type="button" className="flex min-w-0 flex-1 items-center gap-1.5 text-left" onClick={() => activateTab(tab.id)}>
                <BotIcon className="size-3.5 shrink-0" aria-hidden="true" />
                <span className="truncate">{tab.title}</span>
              </button>
              <button
                type="button"
                className="ml-1 rounded p-0.5 opacity-60 hover:bg-muted hover:opacity-100"
                aria-label={`关闭 ${tab.title}`}
                onClick={() => handleCloseTab(tab.id)}
              ><XIcon className="size-3" aria-hidden="true" /></button>
            </div>
          ))}
        </div>
        <Button variant="ghost" size="icon-sm" aria-label="收起 Workbench" onClick={() => setPanelOpen(false)}><PanelRightCloseIcon /></Button>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden" role="tabpanel">
        <WorkbenchSurface tab={activeTab} onClose={() => handleCloseTab(activeTab.id)} />
      </div>
    </aside>
  );
}
