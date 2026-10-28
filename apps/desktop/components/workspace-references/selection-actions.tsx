import type { TextSelection } from "@/components/text-viewer/readonly-text-viewer";
export type { TextSelection } from "@/components/text-viewer/readonly-text-viewer";
import { ContextMenu } from "radix-ui";
import { useReferenceBridge } from "./reference-bridge";
import type { ReactNode } from "react";
import { AddReferenceButton } from "./add-reference-button";
import type { ReferenceInput } from "@/lib/workspace-references/prepare";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import { cn } from "@/lib/utils";
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
            ["添加选区到对话", undefined],
            ["解释", "请解释这段内容。"],
            ["检查问题", "请检查这段内容的潜在问题。"],
            ["设计测试", "请为这段代码设计有意义的测试。"],
          ].map(([label, question]) => (
            <ContextMenu.Item
              key={label}
              disabled={!input || !bridge?.target}
              className={cn(MENU_ITEM_CLASS, "data-[disabled]:opacity-50")}
              onSelect={() =>
                input && void bridge?.add(workspaceId, [input], question)
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

/** 将当前预览中的选区作为引用添加；没有选区时引用整个文件。 */
export function SelectionActions({
  workspaceId,
  path,
  selection = null,
  excerpt = null,
}: {
  workspaceId: number;
  path: string;
  selection?: TextSelection | null;
  excerpt?: string | null;
}) {
  const input = selectedReference(path, selection, excerpt);
  return (
    <AddReferenceButton
      workspaceId={workspaceId}
      inputs={[input ?? { path, kind: "file" }]}
      label={input ? "引用选区" : "引用文件"}
    />
  );
}
