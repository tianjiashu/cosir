import { cn } from "@/lib/utils";

/**
 * 页头图标按钮下沿的状态线，父元素须为 `relative`。
 *
 * 与右上角的 `StatusDot` 分工：点表达「有件事要你注意」（后台任务、终端异常结束、待回车），线表达「这个入口后面
 * 有东西在进行」：`breathing` 是正在进行（缓慢呼吸，复用 `animate-pulse`，「减少动态效果」时由 `globals.css`
 * 统一停住），`static` 是进行完了、留了东西等你看。说明写进按钮的 tooltip 与 aria-label，这里只是视觉提示。
 */
export function StatusLine({ tone, className }: { tone: "breathing" | "static"; className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-status-line={tone}
      className={cn(
        "bg-info absolute bottom-0.5 left-1/2 h-0.5 w-3 -translate-x-1/2 rounded-full",
        tone === "breathing" && "animate-pulse",
        className,
      )}
    />
  );
}
