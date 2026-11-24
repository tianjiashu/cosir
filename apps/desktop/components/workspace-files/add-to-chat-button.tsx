import { MessageSquarePlusIcon } from "lucide-react";

import { useTextSelectionContext } from "@/components/text-selection/use-text-selection";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { shortcutLabel } from "@/lib/keyboard/shortcuts";
import { useReadingCommand } from "@/lib/workspace-files/reading-commands";
import { workspaceFileRef } from "@/lib/workspace-references/ref-capsule";
import { cn } from "@/lib/utils";

/**
 * 阅读工具栏的「添加到对话」，同时承接 Ctrl/Cmd+L：阅读器里有选区时打开摘录评论框（选区浮条，见
 * `components/text-selection`），没有选区时添加整个文件（`<ref>` 指针，正文由 Agent 按路径自己读）。
 * 选区状态由所在阅读器经 `TextSelectionContext` 提供；图片、二进制等没有选区的阅读器只添加整个文件。
 */
export function AddToChatButton({ workspaceId, path }: { workspaceId: number; path: string }) {
  const bridge = useReferenceBridge();
  const selection = useTextSelectionContext();
  const selected = !!selection?.pending;
  const add = () => {
    if (selection?.startComment()) return;
    void bridge?.add(workspaceId, [workspaceFileRef(path)]);
  };
  useReadingCommand("addToChat", add);
  const label = selected ? "摘录选区到对话" : "添加文件到对话";
  return (
    <TooltipIconButton
      tooltip={`${label}（${shortcutLabel("addToChat")}）`}
      aria-label={label}
      disabled={!bridge?.canAdd(workspaceId)}
      className={cn("size-8 shrink-0", selected && "bg-muted text-foreground")}
      onMouseDown={(event) => event.preventDefault()}
      onClick={add}
    >
      <MessageSquarePlusIcon />
    </TooltipIconButton>
  );
}
