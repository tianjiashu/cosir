import { useEffect, useState } from "react";

import { TERMINAL_PANEL_HEIGHT, useLayoutPreferences } from "@/lib/layout-preferences";

function maxHeightForWindow(): number {
  return Math.max(TERMINAL_PANEL_HEIGHT.min, Math.round(window.innerHeight * 0.8));
}

/**
 * 底部终端面板的高度：拖拽调整、展开 / 还原。
 *
 * 只保存用户拖拽出的高度（全局布局偏好，跨 Task 与重启保留，下限由偏好 store 保证）；实际高度在渲染时
 * 派生：展开即上限（窗口高度八成），否则把偏好收敛到上限以内。窗口临时变矮不会改写偏好，变回来即恢复。
 * TaskPage 按 taskId 重建，所以高度不能只放在组件 state 里，否则每切一次任务都会回到默认值。
 */
export function useTerminalPanelHeight() {
  const preferredHeight = useLayoutPreferences((state) => state.terminalPanelHeight);
  const setPreferredHeight = useLayoutPreferences((state) => state.setTerminalPanelHeight);
  const [maxPanelHeight, setMaxPanelHeight] = useState(maxHeightForWindow);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    const updateMaxPanelHeight = () => setMaxPanelHeight(maxHeightForWindow());
    window.addEventListener("resize", updateMaxPanelHeight);
    return () => window.removeEventListener("resize", updateMaxPanelHeight);
  }, []);

  const panelHeight = expanded
    ? maxPanelHeight
    : Math.min(preferredHeight, maxPanelHeight);
  const toggleExpanded = () => setExpanded((current) => !current);
  /** 拖拽调整：退出展开态，以拖拽结果为准。 */
  const resizePanel = (height: number) => {
    setExpanded(false);
    setPreferredHeight(height);
  };

  return { panelHeight, maxPanelHeight, expanded, toggleExpanded, resizePanel };
}
