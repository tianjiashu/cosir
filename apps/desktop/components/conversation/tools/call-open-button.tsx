import { ArrowUpRightIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { REVEAL_ON_DISCLOSURE_HOVER } from "@/components/ui/reveal-on-hover";
import { cn } from "@/lib/utils";

/**
 * 行尾的「打开」：跳到调用对应的资源（Session、终端会话、浏览器标签）。低频入口，所在 `group/disclosure`
 * 被悬停或键盘聚焦时才显示；隐藏时仍占位，显隐不推动行内文字。
 */
export function CallOpenButton({ label, onClick }: {
  /** 读屏名称：打开什么，如「打开分叉会话：前端部分」。 */
  label: string;
  onClick: () => void;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="xs"
      className={cn("text-muted-foreground hover:text-foreground shrink-0", REVEAL_ON_DISCLOSURE_HOVER)}
      onClick={onClick}
      aria-label={label}
    >
      打开<ArrowUpRightIcon aria-hidden="true" />
    </Button>
  );
}
