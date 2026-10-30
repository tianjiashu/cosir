import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

/**
 * 页头图标按钮右上角的数字角标，父元素须为 `relative`。
 *
 * 只是视觉提示（`aria-hidden`）：数字已写进按钮自己的 aria-label，读屏不重复播报。
 * 是否显示、显示什么由调用方决定；`attention` 用于需要用户处理的状态（如终端的「!」）。
 */
export function CountBadge({ tone = "default", className, ...props }: ComponentProps<"span"> & { tone?: "default" | "attention" }) {
  return (
    <span
      aria-hidden="true"
      {...props}
      className={cn(
        "absolute -top-0.5 -right-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full px-0.5 text-2xs leading-none tabular-nums",
        tone === "attention" ? "bg-destructive text-destructive-foreground" : "bg-foreground text-background",
        className,
      )}
    />
  );
}
