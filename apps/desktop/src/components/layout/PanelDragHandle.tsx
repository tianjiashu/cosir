/**
 * 面板拖拽手柄（PanelDragHandle）。
 *
 * 封装 react-resizable-panels 的 Separator，提供工作台统一的
 * 「明显可抓」视觉：常态呈现细分隔线，悬停 / 拖拽时高亮，
 * 并显示纵向抓握点，降低窄命中区带来的误操作。
 *
 * 样式依赖库在 Separator 上输出的 `data-separator` 属性，其值即当前状态
 * （inactive / hover / drag / active / focus / disabled），不自行维护悬停与拖拽状态。
 *
 * @module components/layout/PanelDragHandle
 */

import { Separator } from "react-resizable-panels";
import { GripVertical } from "lucide-react";
import { cn } from "@/lib/utils";

/** PanelDragHandle 组件属性。 */
export interface PanelDragHandleProps {
  /** 附加类名，用于覆盖默认样式。 */
  className?: string;
  /** 无障碍标签，说明该手柄调整的是哪一栏。 */
  ariaLabel: string;
}

/**
 * 工作台竖向面板拖拽手柄。
 *
 * @param props - 组件属性。
 * @param props.className - 附加类名。
 * @param props.ariaLabel - 无障碍标签，供屏幕阅读器区分不同分隔条。
 * @returns 带抓握点的可拖拽分隔条；双击可恢复相邻面板默认宽度。
 */
export function PanelDragHandle({ className, ariaLabel }: PanelDragHandleProps) {
  return (
    <Separator
      aria-label={ariaLabel}
      className={cn(
        // 命中区宽于视觉线宽，保证易抓取
        "group relative flex w-1.5 items-center justify-center",
        "bg-border outline-none transition-colors",
        "data-[separator=hover]:bg-primary/40",
        "data-[separator=focus]:bg-primary/40",
        "data-[separator=drag]:bg-primary/60",
        "data-[separator=active]:bg-primary/60",
        className,
      )}
    >
      {/* 抓握点：常态隐藏，悬停 / 拖拽 / 键盘聚焦时显现 */}
      <div
        className={cn(
          "pointer-events-none flex h-8 w-3 items-center justify-center",
          "rounded-sm border border-border bg-background shadow-sm",
          "opacity-0 transition-opacity",
          "group-data-[separator=hover]:opacity-100",
          "group-data-[separator=focus]:opacity-100",
          "group-data-[separator=drag]:opacity-100",
          "group-data-[separator=active]:opacity-100",
        )}
      >
        <GripVertical className="h-3 w-3 text-muted-foreground" />
      </div>
    </Separator>
  );
}
