import { useEffect, useRef, useState } from "react";

export const MIN_TERMINAL_PANEL_HEIGHT = 192;
const DEFAULT_PANEL_HEIGHT = 320;

/** 底部终端面板的高度：拖拽调整、展开 / 还原，最大值随窗口高度（八成）变化。 */
export function useTerminalPanelHeight() {
  const [panelHeight, setPanelHeight] = useState(DEFAULT_PANEL_HEIGHT);
  const [maxPanelHeight, setMaxPanelHeight] = useState(720);
  const [expanded, setExpanded] = useState(false);
  const restoreHeightRef = useRef(DEFAULT_PANEL_HEIGHT);

  useEffect(() => {
    const updateMaxPanelHeight = () => {
      const nextMaxHeight = Math.max(MIN_TERMINAL_PANEL_HEIGHT, Math.round(window.innerHeight * 0.8));
      setMaxPanelHeight(nextMaxHeight);
      setPanelHeight((current) => Math.min(current, nextMaxHeight));
    };
    updateMaxPanelHeight();
    window.addEventListener("resize", updateMaxPanelHeight);
    return () => window.removeEventListener("resize", updateMaxPanelHeight);
  }, []);

  const clampPanelHeight = (height: number) => Math.min(Math.max(height, MIN_TERMINAL_PANEL_HEIGHT), maxPanelHeight);
  const toggleExpanded = () => {
    if (expanded) {
      setPanelHeight(clampPanelHeight(restoreHeightRef.current));
      setExpanded(false);
      return;
    }
    restoreHeightRef.current = panelHeight;
    setPanelHeight(clampPanelHeight(Math.round(window.innerHeight * 0.72)));
    setExpanded(true);
  };
  /** 拖拽调整：退出展开态，以拖拽结果为准。 */
  const resizePanel = (height: number) => {
    setExpanded(false);
    setPanelHeight(height);
  };

  return { panelHeight, maxPanelHeight, expanded, toggleExpanded, resizePanel };
}
