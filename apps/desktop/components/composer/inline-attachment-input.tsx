"use client";

import {
  useEffect,
  useRef,
  type KeyboardEvent,
} from "react";
import { LOCAL_FILE_TOKEN } from "@/lib/assistant/attachments/local-file-token";

export const FILE_ATTACHMENT_TOKEN_PREFIX = "[[cosir-file:";
export const FILE_ATTACHMENT_TOKEN_SUFFIX = "]]";
export const FILE_ATTACHMENT_TOKEN = LOCAL_FILE_TOKEN;

export type InlineFileAttachment = {
  id: string;
  name: string;
};

type InlineAttachmentInputProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  attachments: readonly InlineFileAttachment[];
  onRemoveAttachment?: (id: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
};

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function renderInlineAttachmentHtml(value: string, attachments: readonly InlineFileAttachment[]): string {
  const byId = new Map(attachments.map((attachment) => [attachment.id, attachment]));
  let cursor = 0;
  let html = "";
  for (const match of value.matchAll(FILE_ATTACHMENT_TOKEN)) {
    const token = match[0];
    const id = match[1] ?? "";
    const offset = match.index ?? cursor;
    html += escapeHtml(value.slice(cursor, offset)).replaceAll("\n", "<br>");
    cursor = offset + token.length;
    const attachment = byId.get(id);
    if (!attachment) {
      html += `<span class="cosir-inline-file-token cosir-inline-file-token-error" data-file-token="true" data-file-id="${escapeHtml(id)}" contenteditable="false"><span class="cosir-inline-file-token-name">附件已失效</span></span>`;
      continue;
    }
    const label = escapeHtml(attachment.name);
    html += `<span class="cosir-inline-file-token" data-file-token="true" data-file-id="${escapeHtml(id)}" contenteditable="false"><span class="cosir-inline-file-token-name">${label}</span><button type="button" class="cosir-inline-file-token-remove" data-file-remove="true" aria-label="移除附件 ${label}">×</button></span>`;
  }
  return html + escapeHtml(value.slice(cursor)).replaceAll("\n", "<br>");
}

function serializeNode(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
  if (node.nodeType !== Node.ELEMENT_NODE) return "";
  const element = node as HTMLElement;
  if (element.dataset.fileToken === "true" && element.dataset.fileId) {
    return `${FILE_ATTACHMENT_TOKEN_PREFIX}${element.dataset.fileId}${FILE_ATTACHMENT_TOKEN_SUFFIX}`;
  }
  if (element.tagName === "BR") return "\n";
  return [...element.childNodes].map(serializeNode).join("");
}

function serializeEditor(element: HTMLElement): string {
  return [...element.childNodes].map(serializeNode).join("");
}

function countPlaceholders(value: string): number {
  return [...value.matchAll(FILE_ATTACHMENT_TOKEN)].length;
}

function appendPlaceholders(value: string, attachments: readonly InlineFileAttachment[]): string {
  const count = attachments.length;
  if (count <= 0) return value;
  const prefix = value.length > 0 && !value.endsWith("\n") ? " " : "";
  const existing = new Set([...value.matchAll(FILE_ATTACHMENT_TOKEN)].map((match) => match[1]));
  const tokens = attachments
    .filter((attachment) => !existing.has(attachment.id))
    .map((attachment) => `${FILE_ATTACHMENT_TOKEN_PREFIX}${attachment.id}${FILE_ATTACHMENT_TOKEN_SUFFIX}`)
    .join("");
  return value + (tokens ? prefix + tokens : "");
}

function removePlaceholder(value: string, id: string): string {
  return value.replace(
    new RegExp(` ?${FILE_ATTACHMENT_TOKEN_PREFIX}${id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}${FILE_ATTACHMENT_TOKEN_SUFFIX}`),
    "",
  );
}

function removeMissingTokens(value: string, attachments: readonly InlineFileAttachment[]): string {
  const valid = new Set(attachments.map((attachment) => attachment.id));
  return value.replace(FILE_ATTACHMENT_TOKEN, (token, id: string) => valid.has(id) ? token : "").trimEnd();
}

export function InlineAttachmentInput({
  value,
  onChange,
  onSubmit,
  attachments,
  onRemoveAttachment,
  placeholder,
  autoFocus = false,
  disabled = false,
  className,
  "aria-label": ariaLabel,
}: InlineAttachmentInputProps) {
  const editorRef = useRef<HTMLDivElement>(null);
  const lastMarkupSignature = useRef("");
  const previousAttachmentCount = useRef<number | null>(null);
  const attachmentSignature = attachments.map((attachment) => `${attachment.id}:${attachment.name}`).join("\u001f");

  useEffect(() => {
    if (autoFocus) editorRef.current?.focus();
  }, [autoFocus]);

  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) return;
    const currentValue = serializeEditor(editor);
    const signature = `${value}\u0000${attachmentSignature}`;
    if (currentValue !== value || lastMarkupSignature.current !== signature) {
        editor.innerHTML = renderInlineAttachmentHtml(value, attachments);
      lastMarkupSignature.current = signature;
    }
  }, [attachmentSignature, attachments, value]);

  useEffect(() => {
    if (previousAttachmentCount.current === attachments.length) return;
    previousAttachmentCount.current = attachments.length;
    const currentCount = countPlaceholders(value);
    if (currentCount < attachments.length) {
      onChange(appendPlaceholders(value, attachments));
    } else if (currentCount > attachments.length) {
      onChange(removeMissingTokens(value, attachments));
    }
  }, [attachments.length, onChange, value]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      onSubmit();
    }
  };

  return (
    <div
      ref={editorRef}
      contentEditable={!disabled}
      suppressContentEditableWarning
      role="textbox"
      aria-label={ariaLabel}
      aria-multiline="true"
      data-placeholder={placeholder}
      className={className}
      onInput={(event) => {
        const nextValue = serializeEditor(event.currentTarget);
        lastMarkupSignature.current = `${nextValue}\u0000${attachmentSignature}`;
        onChange(nextValue);
      }}
      onKeyDown={handleKeyDown}
      onClick={(event) => {
        const target = event.target as HTMLElement;
        const removeButton = target.closest<HTMLElement>("[data-file-remove='true']");
        if (!removeButton || !onRemoveAttachment) return;
        const token = removeButton.closest<HTMLElement>("[data-file-token='true']");
        const id = token?.dataset.fileId;
        if (!id) return;
        event.preventDefault();
        onChange(removePlaceholder(value, id));
        onRemoveAttachment(id);
      }}
      onBlur={(event) => {
        const nextValue = serializeEditor(event.currentTarget);
        if (nextValue !== value) onChange(nextValue);
      }}
    />
  );
}
