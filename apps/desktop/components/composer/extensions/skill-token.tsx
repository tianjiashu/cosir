"use client";

import { Node, mergeAttributes } from "@tiptap/core";
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from "@tiptap/react";
import { HammerIcon } from "lucide-react";
import { InlineToken } from "@/components/ui/inline-token";
import { removeComposerToken } from "./remove-composer-token";
import { useComposerEditorView } from "@/components/composer/composer-editor-context";
import { createSkillMentionToken } from "@/lib/assistant/skill-mention-token";

function SkillTokenView({ node, editor, getPos }: NodeViewProps) {
  const context = useComposerEditorView();
  const name = String(node.attrs.name ?? "");

  return (
    <NodeViewWrapper
      as="span"
      contentEditable={false}
      data-inline-composer-token="true"
      data-skill-token="true"
      data-skill-name={name}
      className="cosir-composer-token-node"
    >
      <InlineToken
        icon={<HammerIcon />}
        name={name}
        description={`Skill：${name}`}
        tone="skill"
        remove={{
          label: `移除 Skill ${name}`,
          disabled: context.disabled,
          onClick: () => removeComposerToken({ node, editor, getPos }),
        }}
      />
    </NodeViewWrapper>
  );
}

export const SkillToken = Node.create({
  name: "skillToken",
  group: "inline",
  inline: true,
  atom: true,
  selectable: false,

  addAttributes() {
    return { name: { default: "" } };
  },

  parseHTML() {
    return [{ tag: "span[data-skill-token]" }];
  },

  renderHTML({ node, HTMLAttributes }) {
    return ["span", mergeAttributes(HTMLAttributes, {
      "data-skill-token": "true",
      "data-skill-name": node.attrs.name,
    }), createSkillMentionToken(String(node.attrs.name ?? ""))];
  },

  renderText({ node }) {
    return createSkillMentionToken(String(node.attrs.name ?? ""));
  },

  addNodeView() {
    return ReactNodeViewRenderer(SkillTokenView);
  },
});
