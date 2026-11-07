import { ChevronRightIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/** 只占透明度不占版面：显隐不推动文字，也不改变虚拟列表的行高。 */
export const REVEAL_ON_HOVER_CLASS = "opacity-0 group-hover/disclosure:opacity-100 group-has-[:focus-visible]/disclosure:opacity-100 [@media(hover:none)]:opacity-100";

/**
 * 开合箭头，收起朝右、展开朝下。`reveal` 时平时隐藏，所在 `group/disclosure` 被悬停或键盘聚焦时才显示，
 * 用于次要入口；收着重要内容的入口（如回合标题）不要用，否则看起来就是一行普通文字。
 * 隐藏时位置仍在，放在文字右侧才不留空白。
 */
export function DisclosureChevron({ open, reveal = false, className }: { open: boolean; reveal?: boolean; className?: string }) {
  return <ChevronRightIcon aria-hidden="true" className={cn("size-3.5 shrink-0 motion-safe:transition-[rotate,opacity]", open && "rotate-90", reveal && REVEAL_ON_HOVER_CLASS, className)} />;
}
