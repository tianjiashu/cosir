import { useCallback, useMemo } from "react";
import { useHotkeys } from "react-hotkeys-hook";

import { useElementSize } from "@/hooks/use-element-size";
import type { DockSizing } from "@/components/layout/dock-frame";
import { PANEL_DOCK_GUTTER } from "@/components/ui/panel-dock";
import { useSessionBrowserTabs } from "@/lib/browser/host-state-store";
import { useBrowserPanelStore } from "@/lib/browser/panel-store";
import { useDockStore } from "@/lib/layout/dock-store";
import { APP_HOTKEY_OPTIONS, APP_SHORTCUTS } from "@/lib/keyboard/shortcuts";
import { useWorkbenchSession } from "@/lib/workbench/workbench-store";
import { DOCK_PANEL_WIDTH, useLayoutPreferences, type DockPanel } from "@/lib/layout/preferences";

const MAIN_MIN_WIDTH = 400;

/**
 * 右侧停靠区在 AppLayout 里的接线：现在显示哪个面板、各面板能否显示、页头按钮状态、宽度与快捷键。
 *
 * 面板的选择在 `useDockStore`，这里只把它与「能不能显示」合起来：文件面板要有工作区且当前页面允许看文件
 * （配置与观测页不行）；工作台要有标签；浏览器显示 `panel-store` 指定的 Session，没指定时跟随当前会话。
 * 不满足条件时面板不渲染，但选择保留，条件恢复后原样出现。宽度：主区与面板共享侧栏之外的宽度，`containerRef`
 * 挂在二者的共同容器上；收窄只限制显示值，不覆盖各面板自己的宽度偏好。
 */
export function useDockPanels({ workspaceId, filesVisible, conversationSessionId }: {
  workspaceId: number | null;
  filesVisible: boolean;
  conversationSessionId: number | null;
}) {
  const selected = useDockStore((state) => state.panel);
  const maximized = useDockStore((state) => state.maximized);
  const workbenchSession = useWorkbenchSession(workspaceId);
  const workbenchTabs = workbenchSession.tabs;
  const pinnedBrowserSession = useBrowserPanelStore((state) => state.sessionId);
  const browserSessionId = pinnedBrowserSession ?? conversationSessionId;
  const conversationTabs = useSessionBrowserTabs(conversationSessionId);

  const available: Record<DockPanel, boolean> = {
    files: filesVisible && workspaceId !== null,
    workbench: workspaceId !== null && workbenchTabs.length > 0,
    browser: true,
  };
  const panel = selected !== null && available[selected] ? selected : null;

  const toggle = useCallback((next: DockPanel) => {
    // 从页头打开浏览器：回到跟随当前会话（卡片上的「打开」可能指定过子 Agent 的 Session）。
    if (next === "browser") useBrowserPanelStore.getState().followConversation();
    useDockStore.getState().toggle(next);
  }, []);
  const close = useCallback(() => useDockStore.getState().close(), []);
  const toggleMaximized = useCallback(() => {
    const store = useDockStore.getState();
    store.setMaximized(!store.maximized);
  }, []);
  useHotkeys(APP_SHORTCUTS.toggleWorkbench.keys, () => toggle("workbench"), { ...APP_HOTKEY_OPTIONS, enabled: available.workbench }, [toggle, available.workbench]);

  const [containerRef, { width: availableWidth }] = useElementSize<HTMLDivElement>();
  const widths = useLayoutPreferences((state) => state.dockWidths);
  const setDockWidth = useLayoutPreferences((state) => state.setDockWidth);
  const sizing = useMemo((): DockSizing | null => {
    if (panel === null) return null;
    const range = DOCK_PANEL_WIDTH[panel];
    const maxWidth = Math.max(0, Math.min(range.max, availableWidth - MAIN_MIN_WIDTH - PANEL_DOCK_GUTTER));
    return {
      width: Math.min(widths[panel], maxWidth),
      minWidth: Math.min(range.min, maxWidth),
      maxWidth,
      onWidthChange: (width: number) => setDockWidth(panel, width),
    };
  }, [availableWidth, panel, setDockWidth, widths]);

  return {
    panel,
    available,
    toggle,
    close,
    workbenchTabs,
    workbenchActiveTabId: workbenchSession.activeTabId,
    browserSessionId,
    /** 页头地球按钮的角标：当前会话的标签数。 */
    conversationTabCount: conversationTabs.length,
    /** 当前面板铺满对话区（主区隐藏）；面板不可显示时不算。 */
    maximized: panel !== null && maximized,
    toggleMaximized,
    containerRef,
    sizing,
  };
}
