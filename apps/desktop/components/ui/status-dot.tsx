import { cn } from "@/lib/utils";

/**
 * 页头图标按钮右上角的状态点，父元素须为 `relative`。
 *
 * 角标只表达状态、不表达数量（与 macOS Dock、VS Code 活动栏一致）：`activity` 是「正在发生」（如后台任务
 * 在运行），`attention` 是「需要你处理」（如终端异常结束且还没看过）。数量与说明写进按钮的 tooltip 和
 * aria-label，这里只是视觉提示（`aria-hidden`）；没有状态时调用方不渲染它。
 */
export function StatusDot({ tone, className }: { tone: "activity" | "attention"; className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-status-dot={tone}
      className={cn(
        "ring-background absolute top-0.5 right-0.5 size-2 rounded-full ring-2",
        tone === "attention" ? "bg-destructive" : "bg-info",
        className,
      )}
    />
  );
}
