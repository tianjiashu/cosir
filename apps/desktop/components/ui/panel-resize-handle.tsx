import { useRef, type PointerEvent } from "react";
import { GripHorizontalIcon, GripVerticalIcon } from "lucide-react";
import { cn } from "@/lib/utils";

const KEYBOARD_STEP = 24;

/**
 * 共享鼠标捕获与键盘调整；尺寸归调用方所有，取消或卸载不遗留全局监听。
 *
 * `orientation` 沿用 ARIA 语义：`vertical` 是竖直分隔条，左右拖动调宽度；
 * `horizontal` 是水平分隔条，上下拖动调高度。`direction=-1` 表示指针沿坐标轴
 * 正向移动时尺寸减小（面板停靠在右侧或底部）；方向键始终跟随分隔条移动方向。
 */
export function PanelResizeHandle({
  value,
  min,
  max,
  onChange,
  orientation = "vertical",
  direction = 1,
  label,
  className,
}: {
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
  orientation?: "vertical" | "horizontal";
  direction?: 1 | -1;
  label: string;
  className?: string;
}) {
  const drag = useRef<{ start: number; value: number } | null>(null);
  const vertical = orientation === "vertical";
  const change = (next: number) => onChange(Math.max(min, Math.min(max, next)));
  const coordinate = (event: PointerEvent) => (vertical ? event.clientX : event.clientY);
  const [backKey, forwardKey] = vertical ? ["ArrowLeft", "ArrowRight"] : ["ArrowUp", "ArrowDown"];
  const GripIcon = vertical ? GripVerticalIcon : GripHorizontalIcon;
  return (
    <div
      role="separator"
      aria-label={label}
      aria-orientation={orientation}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={Math.round(value)}
      tabIndex={0}
      className={cn(
        "z-10 flex shrink-0 touch-none items-center justify-center bg-muted/30 hover:bg-accent focus-ring",
        vertical ? "w-2 cursor-col-resize" : "h-2 cursor-row-resize",
        className,
      )}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        drag.current = { start: coordinate(event), value };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (drag.current)
          change(drag.current.value + direction * (coordinate(event) - drag.current.start));
      }}
      onPointerUp={(event) => {
        drag.current = null;
        event.currentTarget.releasePointerCapture(event.pointerId);
      }}
      onPointerCancel={() => {
        drag.current = null;
      }}
      onLostPointerCapture={() => {
        drag.current = null;
      }}
      onKeyDown={(event) => {
        if (![backKey, forwardKey, "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        change(
          event.key === "Home"
            ? min
            : event.key === "End"
              ? max
              : value + direction * (event.key === backKey ? -KEYBOARD_STEP : KEYBOARD_STEP),
        );
      }}
    >
      <GripIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />
    </div>
  );
}
