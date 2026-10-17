import { Node, mergeAttributes } from "@tiptap/core";
import {
  NodeViewWrapper,
  ReactNodeViewRenderer,
  type NodeViewProps,
} from "@tiptap/react";
import { closeHistory } from "@tiptap/pm/history";
import { ReferenceChip } from "@/components/workspace-references/reference-chip";
import type { WorkspaceReference } from "@/lib/workspace-references/codec";

function ReferenceView({ node, editor, getPos }: NodeViewProps) {
  return (
    <NodeViewWrapper as="span" contentEditable={false}>
      <ReferenceChip
        reference={node.attrs.reference as WorkspaceReference}
        disabled={!editor.isEditable}
        remove={() => {
          const position = getPos();
          if (position === undefined) return;
          // 显式移除独立成一个撤销步骤，不与刚插入的引用合并。
          editor.view.dispatch(
            closeHistory(editor.state.tr).delete(
              position,
              position + node.nodeSize,
            ),
          );
          editor.commands.focus();
        }}
      />
    </NodeViewWrapper>
  );
}
/** 自包含原子节点；撤销恢复整个引用内容，无需注册表补齐数据。 */
export const WorkspaceReferenceNode = Node.create({
  name: "workspaceReference",
  inline: true,
  group: "inline",
  atom: true,
  selectable: true,
  addAttributes: () => ({ reference: { default: null, rendered: false } }),
  parseHTML: () => [],
  renderHTML: ({ HTMLAttributes }) => [
    "span",
    mergeAttributes(HTMLAttributes, { "data-workspace-reference-node": "" }),
  ],
  addNodeView: () => ReactNodeViewRenderer(ReferenceView),
});
