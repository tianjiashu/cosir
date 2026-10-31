import type { ReactNode } from "react";

import { PanelDock } from "@/components/ui/panel-dock";
import { PANEL_HEADER_CLASS } from "@/components/ui/panel-tokens";
import { cn } from "@/lib/utils";

/**
 * 右侧停靠面板（文件、内置浏览器）共用的外框：停靠槽、可拖宽度、卡片外观与头部一行。
 *
 * 头部是标题栏拖动区，并给 Windows 自绘窗口按钮让位（卡片离窗口右缘 8px）；`data-dock-panel` 让页面头部
 * 知道右上角已被卡片占用、不再自己让位（`app-layout.tsx`）。铺满（`fill`）时不再停靠：卡片占满所在行，
 * 宽度偏好保持不变。
 */
export function DockFrame({
  label,
  width,
  minWidth,
  maxWidth,
  onWidthChange,
  fill = false,
  header,
  children,
}: {
  label: string;
  width: number;
  minWidth: number;
  maxWidth: number;
  onWidthChange: (width: number) => void;
  fill?: boolean;
  header: ReactNode;
  children: ReactNode;
}) {
  const card = (
    <section
      role="complementary"
      aria-label={label}
      data-dock-panel
      className={cn("dock-panel bg-background flex min-h-0 min-w-0 flex-col", fill && "flex-1")}
      style={fill ? undefined : { width }}
    >
      <div className={cn("app-drag-region", PANEL_HEADER_CLASS, "pr-[max(0.5rem,calc(var(--titlebar-inset-right)-0.5rem))]")}>
        {header}
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">{children}</div>
    </section>
  );
  if (fill) return <div className="flex min-h-0 min-w-0 flex-1 py-2 pr-2 pl-2">{card}</div>;
  return (
    <PanelDock side="right" size={width} minSize={minWidth} maxSize={maxWidth} onSizeChange={onWidthChange} resizeLabel={`调整${label}宽度`}>
      {card}
    </PanelDock>
  );
}
