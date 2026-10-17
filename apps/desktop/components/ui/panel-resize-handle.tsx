import { useRef } from "react";
import { GripVerticalIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/** 共享鼠标捕获与键盘调整；宽度归调用方所有，取消或卸载不遗留全局监听。 */
export function PanelResizeHandle({
  value,
  min,
  max,
  onChange,
  direction = 1,
  label,
  className,
}: {
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
  direction?: 1 | -1;
  label: string;
  className?: string;
}) {
  const drag = useRef<{ x: number; value: number } | null>(null);
  const change = (next: number) => onChange(Math.max(min, Math.min(max, next)));
  return (
    <div
      role="separator"
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={Math.round(value)}
      tabIndex={0}
      className={cn(
        "z-10 flex w-2 shrink-0 touch-none cursor-col-resize items-center justify-center bg-muted/30 hover:bg-accent focus-ring",
        className,
      )}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        drag.current = { x: event.clientX, value };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (drag.current)
          change(
            drag.current.value + direction * (event.clientX - drag.current.x),
          );
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
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
          return;
        event.preventDefault();
        change(
          event.key === "Home"
            ? min
            : event.key === "End"
              ? max
              : value + direction * (event.key === "ArrowLeft" ? -24 : 24),
        );
      }}
    >
      <GripVerticalIcon
        className="size-3 shrink-0 text-muted-foreground"
        aria-hidden="true"
      />
    </div>
  );
}
