import { useElementSize } from "@/hooks/use-element-size";
import { PANEL_DOCK_GUTTER } from "@/components/ui/panel-dock";
import { useWorkbenchStore } from "@/lib/workbench/store";
import { WORKBENCH_WIDTH } from "@/lib/workspace-preferences";

const MAIN_MIN_WIDTH = 400;

/** 主区与工作台共享侧栏之外的宽度（工作台的停靠留白也计入）；收窄只限制显示值，不覆盖用户的宽度偏好。 */
export function useWorkbenchLayout() {
  const [containerRef, { width: availableWidth }] = useElementSize<HTMLDivElement>();
  const preferredWidth = useWorkbenchStore(state => state.panelWidth);
  const setWidth = useWorkbenchStore(state => state.setPanelWidth);
  const maxWidth = Math.max(0, Math.min(WORKBENCH_WIDTH.max, availableWidth - MAIN_MIN_WIDTH - PANEL_DOCK_GUTTER));
  const minWidth = Math.min(WORKBENCH_WIDTH.min, maxWidth);
  return { containerRef, width: Math.min(preferredWidth, maxWidth), minWidth, maxWidth, setWidth };
}
