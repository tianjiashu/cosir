import { Node, mergeAttributes } from "@tiptap/core";
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from "@tiptap/react";
import {
  basename,
  parseCapsules,
  serializeCapsule,
  type Capsule,
} from "@/lib/assistant/capsules";
import { FileChip, SkillChip } from "@/components/composer/token-chips";
import { ReferenceChip } from "@/components/workspace-references/reference-chip";
import { useComposerEditorView } from "@/components/composer/composer-editor-context";

import { removeComposerToken } from "./remove-composer-token";

/**
 * 输入框里的胶囊是单一原子节点，attrs 直接存 `Capsule`：文件、工作区引用、Skill 共用一套解析、
 * 序列化与剪贴板行为，NodeView 只按 kind 选择对应胶囊外观。
 */
function CapsuleTokenView({ node, editor, getPos }: NodeViewProps) {
  const context = useComposerEditorView();
  const capsule = node.attrs.capsule as Capsule | null;
  if (!capsule) return null;
  const remove = () => removeComposerToken({ node, editor, getPos });

  return (
    <NodeViewWrapper
      as="span"
      contentEditable={false}
      data-capsule-token="true"
      data-capsule-kind={capsule.kind}
      className="cosir-composer-token-node"
    >
      {capsule.kind === "file" ? (
        <FileChip
          path={capsule.path}
          remove={{ label: `移除附件 ${basename(capsule.path)}`, onClick: remove }}
          disabled={context.disabled}
        />
      ) : capsule.kind === "skill" ? (
        <SkillChip
          name={capsule.name}
          remove={{ label: `移除 Skill ${capsule.name}`, onClick: remove }}
          disabled={context.disabled}
        />
      ) : (
        <ReferenceChip capsule={capsule} remove={remove} disabled={context.disabled} />
      )}
    </NodeViewWrapper>
  );
}

/** 从 HTML 剪贴板的 `data-capsule` 恢复胶囊；不合法的内容按普通 HTML 解析。 */
function capsuleFromSerialized(value: string): Capsule | null {
  const segments = parseCapsules(value);
  const [only] = segments;
  return segments.length === 1 && only?.type === "capsule" ? only.capsule : null;
}

export const CapsuleToken = Node.create({
  name: "capsuleToken",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    return { capsule: { default: null, rendered: false } };
  },

  parseHTML() {
    return [
      {
        tag: "span[data-capsule-token]",
        getAttrs: (element) => {
          const capsule = capsuleFromSerialized(element.getAttribute("data-capsule") ?? "");
          return capsule ? { capsule } : false;
        },
      },
    ];
  },

  renderHTML({ node, HTMLAttributes }) {
    const capsule = node.attrs.capsule as Capsule | null;
    const serialized = capsule ? serializeCapsule(capsule) : "";
    return [
      "span",
      mergeAttributes(HTMLAttributes, {
        "data-capsule-token": "true",
        "data-capsule": serialized,
      }),
      serialized,
    ];
  },

  renderText({ node }) {
    const capsule = node.attrs.capsule as Capsule | null;
    return capsule ? serializeCapsule(capsule) : "";
  },

  addNodeView() {
    return ReactNodeViewRenderer(CapsuleTokenView);
  },
});
