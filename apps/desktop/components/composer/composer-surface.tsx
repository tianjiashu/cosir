import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

export const COMPOSER_SURFACE_CLASS_NAME =
  "border-border/60 bg-card focus-within:ring-ring/20 focus-within:ring-2 flex min-w-0 w-full flex-col gap-2 rounded-3xl border p-2 shadow-sm";

/**
 * 输入框底栏图标按钮（附件、常用提示词、Skills、工具）的统一外观：只放图标，名称走 `title` 与
 * `aria-label`。底栏同时容纳模型、上下文与发送，带文字的胶囊会在常见宽度下折成两行。
 */
export const COMPOSER_ICON_BUTTON_CLASS_NAME =
  "text-muted-foreground hover:text-foreground hover:bg-muted-foreground/15 relative inline-flex size-7 shrink-0 items-center justify-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent [&_svg]:size-4";

export function ComposerSurface({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn(COMPOSER_SURFACE_CLASS_NAME, className)} {...props} />;
}
