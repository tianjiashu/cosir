"use client";

import { Node, mergeAttributes } from "@tiptap/core";
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from "@tiptap/react";
import { FileChip } from "@/components/composer/token-chips";
import { removeComposerToken } from "./remove-composer-token";
import { getLocalAttachmentById } from "@/lib/assistant/attachments/local-attachment-registry";
import { useComposerEditorView } from "@/components/composer/composer-editor-context";
import { frontendLog } from "@/lib/logging/frontend-log";

/** 普通文件附件的行内节点；图片不进正文，走输入框上方的附件条。 */
function AttachmentTokenView({ node, editor, getPos }: NodeViewProps) {
  const context = useComposerEditorView();
  const tokenId = String(node.attrs.tokenId ?? "");
  const attachment = getLocalAttachmentById(tokenId);
  const pending = !attachment && context.hydrating;

  function removeAttachment() {
    removeComposerToken({ node, editor, getPos });
    void frontendLog("INFO", "inline_attachment_remove_requested", "请求移除内联附件 token", {
      data: { tokenId, attachmentId: attachment?.id },
    });
  }

  return (
    <NodeViewWrapper
      as="span"
      contentEditable={false}
      data-attachment-token="true"
      data-token-id={tokenId}
      className="cosir-composer-token-node"
    >
      <FileChip
        name={attachment?.name ?? (pending ? "附件同步中" : "附件已失效")}
        description={attachment
          ? attachment.path ?? attachment.name
          : pending ? "附件正在同步" : "附件已失效，请移除后重新选择附件"}
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

const fileToken = (tokenId: unknown) => `[[cosir-file:${String(tokenId ?? "")}]]`;

export const AttachmentToken = Node.create({
  name: "attachmentToken",
  group: "inline",
  inline: true,
  atom: true,
  selectable: false,

  addAttributes() {
    return { tokenId: { default: "" } };
  },

  parseHTML() {
    return [{ tag: "span[data-attachment-token]" }];
  },

  renderHTML({ node, HTMLAttributes }) {
    return ["span", mergeAttributes(HTMLAttributes, {
      "data-attachment-token": "true",
      "data-token-id": node.attrs.tokenId,
    }), fileToken(node.attrs.tokenId)];
  },

  renderText({ node }) {
    return fileToken(node.attrs.tokenId);
  },

  addNodeView() {
    return ReactNodeViewRenderer(AttachmentTokenView);
  },
});
