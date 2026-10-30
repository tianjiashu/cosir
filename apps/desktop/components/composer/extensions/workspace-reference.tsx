import { Node, mergeAttributes } from "@tiptap/core";
import {
  NodeViewWrapper,
  ReactNodeViewRenderer,
  type NodeViewProps,
} from "@tiptap/react";
import { removeComposerToken } from "./remove-composer-token";
import { ReferenceChip } from "@/components/workspace-references/reference-chip";
import type { WorkspaceReference } from "@/lib/workspace-references/codec";
import { useComposerEditorView } from "@/components/composer/composer-editor-context";

function ReferenceView({ node, editor, getPos }: NodeViewProps) {
  const context = useComposerEditorView();
  return (
    <NodeViewWrapper as="span" contentEditable={false} className="cosir-composer-token-node">
      <ReferenceChip
        reference={node.attrs.reference as WorkspaceReference}
        disabled={context.disabled}
        remove={() => removeComposerToken({ node, editor, getPos })}
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
