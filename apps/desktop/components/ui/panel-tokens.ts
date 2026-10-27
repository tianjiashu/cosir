import { cn } from "@/lib/utils";

/**
 * 停靠面板（工作台、终端、文件面板）的头部一行：标题、标签或工具按钮。
 *
 * 刻意不画底线：头部与内容之间靠留白和底色分隔，与应用其余区域「不用结构分割线」一致。
 * 高度固定 h-11，三个面板的头部因此对齐；需要拖动区或安全区让位的调用方自行追加。
 */
export const PANEL_HEADER_CLASS = "flex h-11 shrink-0 items-center gap-1 px-2";

/**
 * 面板标签页（工作台标签、文件阅读标签）的外观：圆角药丸，选中态只换底色。
 * 两处共用同一种样式，不再各自维护方块分隔线与药丸两套写法。
 */
export function panelTabClass(active: boolean, className?: string) {
  return cn(
    "group flex h-8 min-w-0 shrink-0 items-center gap-1 rounded-md pr-1 pl-2 text-xs",
    active ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
    className,
  );
}
