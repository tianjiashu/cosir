"use client";

import { Node, mergeAttributes } from "@tiptap/core";
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from "@tiptap/react";
import { HammerIcon, XIcon } from "lucide-react";
import { useComposerEditorView } from "@/components/composer/composer-editor-context";
import { createSkillMentionToken } from "@/lib/assistant/skill-mention-token";

function SkillTokenView({ node, editor, getPos }: NodeViewProps) {
  const context = useComposerEditorView();
  const name = String(node.attrs.name ?? "");

  function removeSkill() {
    const position = getPos();
    if (typeof position !== "number") return;
    editor.view.dispatch(editor.state.tr.delete(position, position + node.nodeSize));
  }

  return (
    <NodeViewWrapper
      as="span"
      contentEditable={false}
      data-inline-composer-token="true"
      data-skill-token="true"
      data-skill-name={name}
      className="cosir-inline-skill-token"
    >
      <HammerIcon className="cosir-inline-skill-token-icon" aria-hidden="true" />
      <span className="cosir-inline-skill-token-kind">Skill</span>
      <span className="cosir-inline-skill-token-name" title={name}>{name}</span>
      <button
        type="button"
        className="cosir-inline-skill-token-remove"
        data-skill-remove="true"
        aria-label={`移除 Skill ${name}`}
        disabled={context.disabled}
        onMouseDown={(event) => event.preventDefault()}
        onClick={removeSkill}
      ><XIcon aria-hidden="true" /></button>
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
