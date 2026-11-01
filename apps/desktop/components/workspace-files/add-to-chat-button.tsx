import { MessageSquarePlusIcon } from "lucide-react";

import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { selectedReference, type TextSelection } from "@/components/workspace-references/selection-actions";
import { shortcutLabel } from "@/lib/keyboard/shortcuts";
import { useReadingCommand } from "@/lib/workspace-files/reading-commands";
import { cn } from "@/lib/utils";

/**
 * 阅读工具栏的「添加到对话」：有选区时添加选区（源码带行号的片段，或渲染视图 / PDF 里的摘录），否则添加整个
 * 文件。同时承接 Ctrl/Cmd+L。用图标而不用会变的文字区分两种状态：有选区时按钮高亮，提示里写明添加什么。
 */
export function AddToChatButton({ workspaceId, path, selection = null, excerpt = null }: {
  workspaceId: number;
  path: string;
  selection?: TextSelection | null;
  excerpt?: string | null;
}) {
  const bridge = useReferenceBridge();
  const input = selectedReference(path, selection, excerpt);
  const add = () => bridge?.add(workspaceId, [input ?? { path, kind: "file" }]);
  useReadingCommand("addToChat", add);
  const label = input ? "添加选区到对话" : "添加文件到对话";
  return (
    <TooltipIconButton
      tooltip={`${label}（${shortcutLabel("addToChat")}）`}
      aria-label={label}
      disabled={!bridge?.canAdd(workspaceId)}
      className={cn("size-8 shrink-0", input && "bg-muted text-foreground")}
      onClick={add}
    >
      <MessageSquarePlusIcon />
    </TooltipIconButton>
  );
}
