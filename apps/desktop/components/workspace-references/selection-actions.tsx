import type { RefCapsule } from "@/lib/assistant/capsules";
import { ContextMenu } from "radix-ui";
import { useReferenceBridge } from "./reference-bridge";
import type { ReactNode } from "react";
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from "@/components/ui/menu-tokens";
import { cn } from "@/lib/utils";
/**
 * 阅读器正文的右键菜单：把当前选区（一条 `<ref>` 指针）添加到对话，或连同一句预设提问一起添加。没有选区
 * 或没有可用的对话输入框时菜单项禁用（可用性只看 `bridge.canAdd`，真正添加时由 `bridge.add` 再确认）。
 */
export function ReferenceSelectionMenu({
  workspaceId,
  capsule,
  children,
}: {
  workspaceId: number;
  capsule: RefCapsule | null;
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
              disabled={!capsule || !bridge?.canAdd(workspaceId)}
              className={cn(MENU_ITEM_CLASS, "data-[disabled]:opacity-50")}
              onSelect={() => {
                if (capsule) void bridge?.add(workspaceId, [capsule], question);
              }}
            >
              {label}
            </ContextMenu.Item>
          ))}
        </ContextMenu.Content>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}
