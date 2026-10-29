import { useEffect, useState } from "react";

import { readTerminalPanelHeight, TERMINAL_PANEL_HEIGHT, writeTerminalPanelHeight } from "@/lib/workspace-preferences";

export const MIN_TERMINAL_PANEL_HEIGHT = TERMINAL_PANEL_HEIGHT.min;

function maxHeightForWindow(): number {
  return Math.max(MIN_TERMINAL_PANEL_HEIGHT, Math.round(window.innerHeight * 0.8));
}

/**
 * 底部终端面板的高度：拖拽调整、展开 / 还原。
 *
 * 只保存用户拖拽出的高度（偏好，跨 Task 与重启保留）；实际高度在渲染时派生：展开即上限（窗口高度
 * 八成），否则把偏好收敛到 [下限, 上限]。窗口临时变矮不会改写偏好，变回来即恢复。TaskPage 按 taskId
 * 重建，所以高度不能只放在组件 state 里，否则每切一次任务都会回到默认值。
 */
export function useTerminalPanelHeight() {
  const [preferredHeight, setPreferredHeight] = useState(readTerminalPanelHeight);
  const [maxPanelHeight, setMaxPanelHeight] = useState(maxHeightForWindow);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    const updateMaxPanelHeight = () => setMaxPanelHeight(maxHeightForWindow());
    window.addEventListener("resize", updateMaxPanelHeight);
    return () => window.removeEventListener("resize", updateMaxPanelHeight);
  }, []);
  useEffect(() => writeTerminalPanelHeight(preferredHeight), [preferredHeight]);

  const panelHeight = expanded
    ? maxPanelHeight
    : Math.min(Math.max(preferredHeight, MIN_TERMINAL_PANEL_HEIGHT), maxPanelHeight);
  const toggleExpanded = () => setExpanded((current) => !current);
  /** 拖拽调整：退出展开态，以拖拽结果为准。 */
  const resizePanel = (height: number) => {
    setExpanded(false);
    setPreferredHeight(height);
  };

  return { panelHeight, maxPanelHeight, expanded, toggleExpanded, resizePanel };
}
