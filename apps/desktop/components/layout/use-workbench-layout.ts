import { useLayoutEffect, useRef, useState } from "react";
import { PANEL_DOCK_GUTTER } from "@/components/ui/panel-dock";
import { useWorkbenchStore } from "@/lib/workbench/store";

const MAIN_MIN_WIDTH = 400;
const PANEL_MIN_WIDTH = 320;
const PANEL_MAX_WIDTH = 900;

/** 主区与工作台共享侧栏之外的宽度（工作台的停靠留白也计入）；收窄只限制显示值，不覆盖用户的宽度偏好。 */
export function useWorkbenchLayout() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [availableWidth, setAvailableWidth] = useState(0);
  const preferredWidth = useWorkbenchStore(state => state.panelWidth);
  const setWidth = useWorkbenchStore(state => state.setPanelWidth);
  useLayoutEffect(() => {
    const container = containerRef.current!;
    const measure = () => setAvailableWidth(container.getBoundingClientRect().width);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);
  const maxWidth = Math.max(0, Math.min(PANEL_MAX_WIDTH, availableWidth - MAIN_MIN_WIDTH - PANEL_DOCK_GUTTER));
  const minWidth = Math.min(PANEL_MIN_WIDTH, maxWidth);
  return { containerRef, width: Math.min(preferredWidth, maxWidth), minWidth, maxWidth, setWidth };
}
