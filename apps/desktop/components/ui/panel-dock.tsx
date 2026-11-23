import type { ReactNode } from "react";

import { PanelResizeHandle } from "@/components/ui/panel-resize-handle";
import { cn } from "@/lib/utils";

/** 槽在停靠方向上额外占用的尺寸：手柄所在的缝（w-2/h-2）加外侧留白（pr-2/pb-2）。 */
export const PANEL_DOCK_GUTTER = 16;

/**
 * 停靠面板的布局槽：负责面板与窗口、主区之间的留白，并把调整尺寸的手柄放进
 * 面板与主区之间的那道缝里——留白本身就是拖拽热区，二者因此归同一个组件。
 *
 * 只管位置不管外观：卡片外观由子元素自带 `dock-panel` 工具类并自行设置宽或高；
 * 卡片带 `overflow-hidden` 裁剪圆角，所以手柄必须在卡片之外，不能放进卡片里。
 * 尺寸状态归调用方所有，这里只转发给手柄。
 *
 * `size="fill"`：槽占满所在行（面板铺满），没有可拖的尺寸，所以不渲染手柄、缝换成同宽留白。
 * 两种尺寸共用同一层外壳、手柄在原位条件渲染，子元素的位置不变：切换铺满不会卸载重挂卡片内容
 * （否则面板内的滚动、查找等状态丢失，绑在卡片内节点上的尺寸观察会盯着已卸载的旧节点）。
 */
export function PanelDock({
  side,
  size,
  minSize,
  maxSize,
  onSizeChange,
  resizeLabel,
  children,
}: {
  side: "right" | "bottom";
  size: number | "fill";
  minSize: number;
  maxSize: number;
  onSizeChange: (size: number) => void;
  resizeLabel: string;
  children: ReactNode;
}) {
  const right = side === "right";
  const fill = size === "fill";
  return (
    <div
      className={cn(
        right ? "flex min-h-0 py-2 pr-2" : "flex min-w-0 flex-col px-2 pb-2",
        fill ? (right ? "min-w-0 flex-1 pl-2" : "min-h-0 flex-1 pt-2") : "shrink-0",
      )}
    >
      {!fill && (
        <PanelResizeHandle
          // 缝里的拖拽图标只在悬停或键盘聚焦时出现，平时只留一道干净的缝。
          className="bg-transparent [&>svg]:opacity-0 hover:[&>svg]:opacity-100 focus-visible:[&>svg]:opacity-100"
          label={resizeLabel}
          orientation={right ? "vertical" : "horizontal"}
          direction={-1}
          value={size}
          min={minSize}
          max={maxSize}
          onChange={onSizeChange}
        />
      )}
      {children}
    </div>
  );
}
