import { ArrowUpRightIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { REVEAL_ON_HOVER_CLASS } from "@/components/ui/disclosure-chevron";
import { cn } from "@/lib/utils";

/**
 * 行尾的「打开」：跳到调用对应的资源（Task、终端会话、浏览器标签）。低频入口，所在 `group/disclosure`
 * 被悬停或键盘聚焦时才显示；隐藏时仍占位，显隐不推动行内文字。
 */
export function CallOpenButton({ label, onClick }: {
  /** 读屏名称：打开什么，如「打开分叉对话：前端部分」。 */
  label: string;
  onClick: () => void;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="xs"
      className={cn("text-muted-foreground hover:text-foreground shrink-0", REVEAL_ON_HOVER_CLASS)}
      onClick={onClick}
      aria-label={label}
    >
      打开<ArrowUpRightIcon aria-hidden="true" />
    </Button>
  );
}
