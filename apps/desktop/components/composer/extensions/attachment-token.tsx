"use client";

import { Node, mergeAttributes } from "@tiptap/core";
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from "@tiptap/react";
import { FileTextIcon, ImageIcon, XIcon } from "lucide-react";
import { reconcileComposerAttachments, serializeComposerDocument, type InlineComposerAttachment } from "@/components/composer/composer-document";
import { useComposerEditorView } from "@/components/composer/composer-editor-context";
import { frontendLog } from "@/lib/logging/frontend-log";

function AttachmentTokenView({ node, editor, getPos }: NodeViewProps) {
  const context = useComposerEditorView();
  const kind = node.attrs.kind === "image" ? "image" : "file";
  const tokenId = String(node.attrs.tokenId ?? "");
  const attachment = context.attachments.find((item) =>
    (item.kind ?? "file") === kind && (item.tokenId ?? item.id) === tokenId,
  );
  const pending = !attachment && context.unmatchedAsPending;
  const Icon = kind === "image" ? ImageIcon : FileTextIcon;

  function removeAttachment() {
    if (!attachment) return;
    if (!context.onRemoveAttachment) {
      void frontendLog("WARNING", "inline_attachment_remove_unavailable", "点击移除附件，但当前输入框没有移除回调", {
        data: { disabled: context.disabled },
      });
      return;
    }
    const position = getPos();
    if (typeof position !== "number") return;
    const currentText = serializeComposerDocument(editor.getJSON());
    const from = editor.state.doc.textBetween(Math.max(0, position - 1), position) === " "
      ? position - 1
      : position;
    const transaction = editor.state.tr.delete(from, position + node.nodeSize);
    editor.view.dispatch(transaction);
    const nextText = serializeComposerDocument(editor.getJSON());
    const remainingTokenKeys = reconcileComposerAttachments(nextText, context.attachments).tokenKeys;
    void frontendLog("INFO", "inline_attachment_remove_requested", "请求移除内联附件 token", {
      data: {
        kind,
        tokenId,
        attachmentId: attachment.id,
        textLengthBefore: currentText.length,
        textLengthAfter: nextText.length,
        remainingTokenKeys,
      },
    });
    context.onRemoveAttachment(attachment.id);
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
      className={`cosir-inline-attachment-token ${kind === "image" ? "cosir-inline-image-token" : "cosir-inline-file-token"} ${attachment ? "" : pending ? "cosir-inline-file-token-pending" : "cosir-inline-file-token-error"}`}
    >
      <Icon className="cosir-inline-file-token-icon" aria-hidden="true" />
      <span className="cosir-inline-file-token-name">
        {attachment ? `${kind === "image" ? "图片：" : ""}${attachment.name}` : pending ? "附件同步中" : "附件已失效"}
      </span>
      {attachment && (
        <button
          type="button"
          className="cosir-inline-file-token-remove"
          data-file-remove="true"
          aria-label={`移除附件 ${attachment.name}`}
          disabled={context.disabled || !context.onRemoveAttachment}
          onMouseDown={(event) => event.preventDefault()}
          onClick={removeAttachment}
        ><XIcon aria-hidden="true" /></button>
      )}
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
