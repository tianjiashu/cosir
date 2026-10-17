"use client";

import { Node, mergeAttributes } from "@tiptap/core";
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from "@tiptap/react";
import { FileTextIcon, ImageIcon } from "lucide-react";
import { InlineToken } from "@/components/ui/inline-token";
import { removeComposerToken } from "./remove-composer-token";
import { getLocalAttachmentById } from "@/lib/assistant/attachments/local-attachment-registry";
import { useComposerEditorView } from "@/components/composer/composer-editor-context";
import { frontendLog } from "@/lib/logging/frontend-log";

function AttachmentTokenView({ node, editor, getPos }: NodeViewProps) {
  const context = useComposerEditorView();
  const kind = node.attrs.kind === "image" ? "image" : "file";
  const tokenId = String(node.attrs.tokenId ?? "");
  const attachment = getLocalAttachmentById(tokenId);
  const pending = !attachment && context.hydrating;
  const Icon = kind === "image" ? ImageIcon : FileTextIcon;

  function removeAttachment() {
    removeComposerToken({ node, editor, getPos });
    void frontendLog("INFO", "inline_attachment_remove_requested", "请求移除内联附件 token", {
      data: {
        kind,
        tokenId,
        attachmentId: attachment?.id,
      },
    });
  }

  return (
    <NodeViewWrapper
      as="span"
      contentEditable={false}
      data-attachment-token="true"
      data-attachment-kind={kind}
      data-token-id={tokenId}
      data-attachment-id={attachment?.id}
      data-file-id={kind === "file" ? attachment?.id : undefined}
      className="cosir-composer-token-node"
    >
      <InlineToken
        icon={<Icon />}
        name={attachment?.name ?? (pending ? "附件同步中" : "附件已失效")}
        description={attachment
          ? attachment.path ?? attachment.name
          : pending ? "附件正在同步" : "附件已失效，请移除后重新选择附件"}
        preserveExtension={!!attachment}
        state={attachment ? "ready" : pending ? "pending" : "error"}
        remove={{
          label: attachment ? `移除附件 ${attachment.name}` : "移除附件",
          disabled: context.disabled,
          onClick: removeAttachment,
        }}
      />
    </NodeViewWrapper>
  );
}

export const AttachmentToken = Node.create({
  name: "attachmentToken",
  group: "inline",
  inline: true,
  atom: true,
  selectable: false,

  addAttributes() {
    return {
      kind: { default: "file" },
      tokenId: { default: "" },
    };
  },

  parseHTML() {
    return [{ tag: "span[data-attachment-token]" }];
  },

  renderHTML({ node, HTMLAttributes }) {
    const kind = node.attrs.kind === "image" ? "image" : "file";
    return ["span", mergeAttributes(HTMLAttributes, {
      "data-attachment-token": "true",
      "data-attachment-kind": kind,
      "data-token-id": node.attrs.tokenId,
    }), `[[cosir-${kind}:${node.attrs.tokenId}]]`];
  },

  renderText({ node }) {
    const kind = node.attrs.kind === "image" ? "image" : "file";
    return `[[cosir-${kind}:${node.attrs.tokenId}]]`;
  },

  addNodeView() {
    return ReactNodeViewRenderer(AttachmentTokenView);
  },
});
