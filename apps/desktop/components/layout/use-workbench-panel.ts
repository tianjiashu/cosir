import { useCallback, useMemo } from "react";
import { useHotkeys } from "react-hotkeys-hook";

import { useElementSize } from "@/hooks/use-element-size";
import { PANEL_DOCK_GUTTER } from "@/components/ui/panel-dock";
import { APP_HOTKEY_OPTIONS, APP_SHORTCUTS } from "@/lib/shortcuts";
import { useWorkbenchSession, useWorkbenchStore } from "@/lib/workbench/store";
import { useLayoutPreferences, WORKBENCH_WIDTH } from "@/lib/layout-preferences";

const MAIN_MIN_WIDTH = 400;

/**
 * 工作台面板在 AppLayout 里的接线：显示哪个工作区的桶、哪些标签可见、开合、宽度与快捷键。
 *
 * - `workspaceId` 为 null（归属尚在加载）时暂不显示，不销毁任何内容。
 * - `filesVisible` 为 false（配置与观测页）时过滤掉文件标签；这是标签可见性的唯一过滤处，
 *   工作台与页头按钮共用结果，面板「打开」也以过滤后仍有标签为准。
 * - 收起只改变可见性；草稿由工作台会话保留，真正销毁只在关闭标签或删除工作区 / Task 时发生。
 * - 宽度：主区与工作台共享侧栏之外的宽度（工作台的停靠留白也计入），`containerRef` 挂在二者的
 *   共同容器上；收窄只限制显示值，不覆盖用户的宽度偏好。
 */
export function useWorkbenchPanel(workspaceId: number | null, filesVisible: boolean) {
  const session = useWorkbenchSession(workspaceId);
  const tabs = useMemo(
    () => session.tabs.filter((tab) => filesVisible || tab.kind !== "files"),
    [filesVisible, session.tabs],
  );
  const open = session.panelOpen && tabs.length > 0;
  // 文件标签每个工作区至多一个，id 固定；「文件」图标在它处于前台时高亮。
  const filesActive = open && session.activeTabId === "workspace-files";

  const setOpen = useCallback((next: boolean) => {
    if (workspaceId !== null) useWorkbenchStore.getState().setPanelOpen(workspaceId, next);
  }, [workspaceId]);
  const toggle = useCallback(() => setOpen(!open), [open, setOpen]);
  const openFiles = useCallback(() => {
    if (workspaceId !== null) useWorkbenchStore.getState().openFiles(workspaceId);
  }, [workspaceId]);
  useHotkeys(APP_SHORTCUTS.toggleWorkbench.keys, toggle, { ...APP_HOTKEY_OPTIONS, enabled: tabs.length > 0 }, [toggle]);

  const [containerRef, { width: availableWidth }] = useElementSize<HTMLDivElement>();
  const preferredWidth = useLayoutPreferences((state) => state.workbenchWidth);
  const setWidth = useLayoutPreferences((state) => state.setWorkbenchWidth);
  const maxWidth = Math.max(0, Math.min(WORKBENCH_WIDTH.max, availableWidth - MAIN_MIN_WIDTH - PANEL_DOCK_GUTTER));
  const minWidth = Math.min(WORKBENCH_WIDTH.min, maxWidth);

  return {
    tabs,
    activeTabId: session.activeTabId,
    open,
    filesActive,
    setOpen,
    toggle,
    openFiles,
    containerRef,
    width: Math.min(preferredWidth, maxWidth),
    minWidth,
    maxWidth,
    setWidth,
  };
}
