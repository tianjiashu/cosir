import { CodeXmlIcon, EyeIcon } from "lucide-react";

import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { cn } from "@/lib/utils";

/** 渲染视图与源码之间的一键切换（文件阅读器、Markdown 编辑器共用）：图标表示点下去会去哪一边。 */
export function ViewToggle({ rendered, onToggle, className }: {
  rendered: boolean;
  onToggle: () => void;
  className?: string;
}) {
  const label = rendered ? "查看源码" : "查看预览";
  return (
    <TooltipIconButton tooltip={label} aria-label={label} className={cn("size-8 shrink-0", className)} onClick={onToggle}>
      {rendered ? <CodeXmlIcon /> : <EyeIcon />}
    </TooltipIconButton>
  );
}
