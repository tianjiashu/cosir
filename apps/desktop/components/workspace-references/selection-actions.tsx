import type { TextSelection } from "@/components/text-viewer/readonly-text-viewer";
export type { TextSelection } from "@/components/text-viewer/readonly-text-viewer";
import { ContextMenu } from "radix-ui";
import { useReferenceBridge } from "./reference-bridge";
import type { ReactNode } from "react";
import type { ReferenceInput } from "@/lib/workspace-references/prepare";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import { cn } from "@/lib/utils";
/**
 * 阅读器正文的右键菜单：把当前选区添加到对话，或连同一句预设提问一起添加。没有选区或没有可用的对话输入框时
 * 菜单项禁用（可用性只看 `bridge.canAdd`，真正添加时由 `bridge.add` 再确认）。
 */
export function ReferenceSelectionMenu({
  workspaceId,
  input,
  children,
}: {
  workspaceId: number;
  input: ReferenceInput | null;
  children: ReactNode;
}) {
  const bridge = useReferenceBridge();
  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger asChild>{children}</ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Content className={cn(MENU_CONTENT_CLASS, "min-w-40")}>
          {[
            ["添加到对话", undefined],
            ["解释", "请解释这段内容。"],
            ["检查问题", "请检查这段内容的潜在问题。"],
            ["设计测试", "请为这段代码设计有意义的测试。"],
          ].map(([label, question]) => (
            <ContextMenu.Item
              key={label}
              disabled={!input || !bridge?.canAdd(workspaceId)}
              className={cn(MENU_ITEM_CLASS, "data-[disabled]:opacity-50")}
              onSelect={() =>
                input && bridge?.add(workspaceId, [input], question)
              }
            >
              {label}
            </ContextMenu.Item>
          ))}
        </ContextMenu.Content>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}

export function selectedReference(
  path: string,
  selection: TextSelection | null,
  excerpt: string | null,
): ReferenceInput | null {
  if (excerpt) return { path, kind: "excerpt", content: excerpt };
  if (selection)
    return {
      path,
      kind: "snippet",
      content: selection.text,
      ...(selection.startLine !== undefined && selection.endLine !== undefined
        ? {
            start_line: selection.startLine,
            end_line: selection.endLine,
          }
        : {}),
    };
  return null;
}
