import { MessageSquarePlusIcon } from "lucide-react";

import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { useReferenceBridge } from "@/components/workspace-references/reference-bridge";
import { shortcutLabel } from "@/lib/keyboard/shortcuts";
import { useReadingCommand } from "@/lib/workspace-files/reading-commands";
import type { RefCapsule } from "@/lib/assistant/capsules";
import { workspaceFileRef } from "@/lib/workspace-references/ref-capsule";
import { cn } from "@/lib/utils";

/**
 * 阅读工具栏的「添加到对话」：有选区时添加选区指针（源码行号，或渲染视图 / PDF 里的行 / 页），否则添加
 * 整个文件。同时承接 Ctrl/Cmd+L。用图标而不用会变的文字区分两种状态：有选区时按钮高亮，提示里写明
 * 添加什么。引用只带路径与定位，正文由 Agent 按路径自己读。
 */
export function AddToChatButton({ workspaceId, path, selection = null }: {
  workspaceId: number;
  path: string;
  selection?: RefCapsule | null;
}) {
  const bridge = useReferenceBridge();
  const add = () => {
    void bridge?.add(workspaceId, [selection ?? workspaceFileRef(path)]);
  };
  useReadingCommand("addToChat", add);
  const label = selection ? "添加选区到对话" : "添加文件到对话";
  return (
    <TooltipIconButton
      tooltip={`${label}（${shortcutLabel("addToChat")}）`}
      aria-label={label}
      disabled={!bridge?.canAdd(workspaceId)}
      className={cn("size-8 shrink-0", selection && "bg-muted text-foreground")}
      onClick={add}
    >
      <MessageSquarePlusIcon />
    </TooltipIconButton>
  );
}
