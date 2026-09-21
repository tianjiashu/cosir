"use client";

import {
  createContext,
  useEffect,
  useCallback,
  useContext,
  useMemo,
  useRef,
  type KeyboardEvent,
  type PropsWithChildren,
} from "react";
import { inlineAttachmentKey, LOCAL_FILE_TOKEN } from "@/lib/assistant/attachments/local-file-token";
import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";

export const FILE_ATTACHMENT_TOKEN_PREFIX = "[[cosir-file:";
export const FILE_ATTACHMENT_TOKEN_SUFFIX = "]]";
export const FILE_ATTACHMENT_TOKEN = LOCAL_FILE_TOKEN;
const INLINE_ATTACHMENT_TOKEN = /\[\[cosir-(file|image):([^\]]+)\]\]/g;
const HIDDEN_ATTACHMENT_TOKEN = /<!--\s*(\[\[cosir-(?:file|image):[^\]]+\]\])\s*-->/g;
const UNSUPPORTED_EMBEDDED_CONTENT_SELECTOR = "img,video,audio,canvas,iframe,object,embed";

const FILE_ICON_SVG = `<svg class="cosir-inline-file-token-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/></svg>`;

export type InlineFileAttachment = {
  id: string;
  name: string;
  kind?: "file" | "image";
  tokenId?: string;
};

type InlineAttachmentInsertionContextValue = {
  register: (handler: (attachments: readonly InlineFileAttachment[]) => void) => () => void;
  insert: (attachments: readonly InlineFileAttachment[]) => void;
};

const InlineAttachmentInsertionContext = createContext<InlineAttachmentInsertionContextValue | null>(null);

/**
 * Connect an attachment picker to the contenteditable that owns token order.
 *
 * The picker is asynchronous, so the input captures its logical caret offset
 * before the picker opens and inserts the selected file token at that offset.
 * This deliberately keeps attachment insertion event-driven; attachment
 * counts are not used to infer text mutations.
 */
export function InlineAttachmentInsertionProvider({ children }: PropsWithChildren) {
  const handlerRef = useRef<((attachments: readonly InlineFileAttachment[]) => void) | null>(null);
  const register = useCallback((handler: (attachments: readonly InlineFileAttachment[]) => void) => {
    handlerRef.current = handler;
    return () => {
      if (handlerRef.current === handler) handlerRef.current = null;
    };
  }, []);
  const insert = useCallback((attachments: readonly InlineFileAttachment[]) => {
    handlerRef.current?.(attachments);
  }, []);
  const contextValue = useMemo(() => ({ register, insert }), [insert, register]);

  return (
    <InlineAttachmentInsertionContext.Provider value={contextValue}>
      {children}
    </InlineAttachmentInsertionContext.Provider>
  );
}

export function useInlineAttachmentInsertion(): InlineAttachmentInsertionContextValue {
  const context = useContext(InlineAttachmentInsertionContext);
  if (!context) {
    return {
      register: () => () => undefined,
      insert: () => undefined,
    };
  }
  return context;
}

type InlineAttachmentInputProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  attachments: readonly InlineFileAttachment[];
  onExternalFiles?: (files: readonly File[]) => readonly InlineFileAttachment[] | Promise<readonly InlineFileAttachment[]>;
  onRemoveAttachment?: (id: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  disabled?: boolean;
  suspendAttachmentReconciliation?: boolean;
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

type InlineAttachmentRenderOptions = {
  /**
   * 为 true 时把「找不到附件对象」的 token 渲染为「附件同步中」，而不是「附件已失效」。
   * 用于草稿正在写入 composer 的过渡窗口，避免把正常的同步延迟误报为失效。
   */
  unmatchedAsPending?: boolean;
};

/**
 * 把可编辑文本渲染为带附件胶囊的 HTML。
 *
 * 负责什么：解包隐藏注释形式的内联 token，按 token 在文本中的顺序生成不可编辑的胶囊
 * 节点，并转义其余文本。胶囊 DOM 携带 `data-attachment-token`、`data-attachment-kind`、
 * `data-token-id`，匹配到附件时额外携带 `data-attachment-id`，供序列化与移除逻辑使用。
 * 不负责什么：不修改文本、不写 composer、不判断附件是否真的可用。
 *
 * 参数:
 *     value: 可编辑文本，可含 `[[cosir-file:id]]` / `[[cosir-image:id]]` 或注释包裹形式。
 *     attachments: 当前 composer 里的附件视图，以 `kind:tokenId` 与 token 对应。
 *     options.unmatchedAsPending: 未匹配 token 按「同步中」还是「已失效」呈现。
 *
 * 返回:
 *     可直接写入 `innerHTML` 的 HTML 字符串。
 *
 * 异常/副作用:
 *     无；纯函数。
 */
export function renderInlineAttachmentHtml(
  value: string,
  attachments: readonly InlineFileAttachment[],
  options: InlineAttachmentRenderOptions = {},
): string {
  // Canonical message rendering hides internal tokens in HTML comments. The
  // editor removes only that wrapper before creating a typed chip; the DOM
  // serializer still emits the original token for edit/resend recovery.
  const editorValue = value.replace(HIDDEN_ATTACHMENT_TOKEN, "$1");
  const byToken = new Map(attachments.map((attachment) => [
    inlineAttachmentKey(attachment.kind ?? "file", attachment.tokenId ?? attachment.id),
    attachment,
  ]));
  let cursor = 0;
  let html = "";
  for (const match of editorValue.matchAll(INLINE_ATTACHMENT_TOKEN)) {
    const token = match[0];
    const kind = match[1] ?? "file";
    const tokenId = match[2] ?? "";
    const offset = match.index ?? cursor;
    html += escapeHtml(editorValue.slice(cursor, offset)).replaceAll("\n", "<br>");
    cursor = offset + token.length;
    const attachment = byToken.get(`${kind}:${tokenId}`);
    if (!attachment) {
      const pending = options.unmatchedAsPending === true;
      const stateClass = pending ? "cosir-inline-file-token-pending" : "cosir-inline-file-token-error";
      const stateLabel = pending ? "附件同步中" : "附件已失效";
      html += `<span class="cosir-inline-attachment-token ${stateClass}" data-attachment-token="true" data-attachment-kind="${kind}" data-token-id="${escapeHtml(tokenId)}" contenteditable="false">${FILE_ICON_SVG}<span class="cosir-inline-file-token-name">${stateLabel}</span></span>`;
      continue;
    }
    const label = escapeHtml(attachment.name);
    const isImage = kind === "image";
    html += `<span class="cosir-inline-attachment-token ${isImage ? "cosir-inline-image-token" : "cosir-inline-file-token"}" data-attachment-token="true" data-attachment-kind="${kind}" data-attachment-id="${escapeHtml(attachment.id)}"${kind === "file" ? ` data-file-id="${escapeHtml(attachment.id)}"` : ""} data-token-id="${escapeHtml(tokenId)}" contenteditable="false">${FILE_ICON_SVG}<span class="cosir-inline-file-token-name">${isImage ? "图片：" : ""}${label}</span><button type="button" class="cosir-inline-file-token-remove" data-file-remove="true" aria-label="移除附件 ${label}">×</button></span>`;
  }
  return html + escapeHtml(editorValue.slice(cursor)).replaceAll("\n", "<br>");
}

/**
 * 列出输入文本中所有内联附件 token 的 `kind:id` 键（先解包隐藏注释）。
 *
 * 供对账诊断使用：与 `attachments` 提供的键集合比较，即可判断哪些 token 会落入
 * 「附件已失效」兜底分支。只解析结构，不修改文本，也不产生副作用。
 */
function textAttachmentTokenKeys(value: string): string[] {
  const editorValue = value.replace(HIDDEN_ATTACHMENT_TOKEN, "$1");
  return [...editorValue.matchAll(INLINE_ATTACHMENT_TOKEN)].map((match) =>
    inlineAttachmentKey(match[1] === "image" ? "image" : "file", match[2] ?? ""),
  );
}

function serializeNode(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
  if (node.nodeType !== Node.ELEMENT_NODE) {
    return [...node.childNodes].map(serializeNode).join("");
  }
  const element = node as HTMLElement;
  if (element.dataset.attachmentToken === "true" && element.dataset.attachmentKind && element.dataset.tokenId) {
    return `[[cosir-${element.dataset.attachmentKind}:${element.dataset.tokenId}]]`;
  }
  if (element.tagName === "BR") return "\n";
  return [...element.childNodes].map(serializeNode).join("");
}

function serializeEditor(element: HTMLElement): string {
  return [...element.childNodes].map(serializeNode).join("");
}

/**
 * Remove browser-native embedded media from the editable surface.
 *
 * The composer stores attachments separately from editable text. Browsers can
 * nevertheless insert an `<img>` (for example when a screenshot is pasted)
 * into a contenteditable element. The text serializer intentionally ignores
 * that node, so leaving it in the DOM would make its intrinsic dimensions
 * participate in layout while the controlled value remains unchanged.
 *
 * Returns the removed tag names for diagnostic logging. It does not touch
 * inline attachment tokens, which are represented by dedicated spans.
 */
function removeUnsupportedEmbeddedContent(element: HTMLElement): string[] {
  const nodes = [...element.querySelectorAll<HTMLElement>(UNSUPPORTED_EMBEDDED_CONTENT_SELECTOR)];
  const tagNames = nodes.map((node) => node.tagName.toLowerCase());
  nodes.forEach((node) => node.remove());
  return tagNames;
}

function removePlaceholder(value: string, kind: "file" | "image", tokenId: string): string {
  const escapedId = tokenId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const hiddenToken = new RegExp(
    ` ?<!--\\s*\\[\\[cosir-${kind}:${escapedId}\\]\\]\\s*-->`,
  );
  const plainToken = new RegExp(
    ` ?\\[\\[cosir-${kind}:${escapedId}\\]\\]`,
  );
  return value.replace(hiddenToken, "").replace(plainToken, "");
}

export function InlineAttachmentInput({
  value,
  onChange,
  onSubmit,
  attachments,
  onExternalFiles,
  onRemoveAttachment,
  placeholder,
  autoFocus = false,
  disabled = false,
  suspendAttachmentReconciliation = false,
  className,
  "aria-label": ariaLabel,
}: InlineAttachmentInputProps) {
  const editorRef = useRef<HTMLDivElement>(null);
  const lastMarkupSignature = useRef("");
  const caretOffset = useRef<number | null>(null);
  const currentValue = useRef(value);
  const insertion = useInlineAttachmentInsertion();
  const attachmentSignature = attachments.map((attachment) => `${attachment.id}:${attachment.name}`).join("\u001f");

  currentValue.current = value;

  // 文本 token 与附件对象是两条独立写入路径，任一侧滞后都会影响内联胶囊的呈现。这里
  // 统一算一次对账结果，供渲染（区分「同步中」与「已失效」）与诊断日志共用。
  // 依赖包含 `attachments`（每次渲染的新数组）：键计算必须读取附件内容，无法避免；
  // 重算成本可忽略，真正用于去抖的是下游日志 effect 的签名比较。
  const reconciliation = useMemo(() => {
    const attachmentKeys = attachments.map(
      (attachment) => inlineAttachmentKey(attachment.kind ?? "file", attachment.tokenId ?? attachment.id),
    );
    const tokenKeys = textAttachmentTokenKeys(value);
    const attachmentKeySet = new Set(attachmentKeys);
    const tokenKeySet = new Set(tokenKeys);
    return {
      attachmentKeys,
      tokenKeys,
      unmatchedTokenKeys: tokenKeys.filter((key) => !attachmentKeySet.has(key)),
      tokenlessAttachmentKeys: attachmentKeys.filter((key) => !tokenKeySet.has(key)),
    };
  }, [attachmentSignature, attachments, value]);

  const lastReconcileSignature = useRef("");

  // 对账诊断：仅在签名变化时记录一次，避免叠加渲染噪音。
  useEffect(() => {
    const { attachmentKeys, tokenKeys, unmatchedTokenKeys, tokenlessAttachmentKeys } = reconciliation;
    const signature = `${unmatchedTokenKeys.join(",")}|${tokenlessAttachmentKeys.join(",")}|${suspendAttachmentReconciliation}|${disabled}`;
    if (signature === lastReconcileSignature.current) return;
    lastReconcileSignature.current = signature;

    if (unmatchedTokenKeys.length > 0) {
      const stage = suspendAttachmentReconciliation ? "同步中" : "已失效";
      void frontendLog("WARNING", "inline_attachment_token_unmatched", `输入框存在没有附件对象的 token，渲染为「附件${stage}」`, {
        data: {
          unmatchedTokenKeys,
          attachmentKeys,
          tokenKeys,
          suspendAttachmentReconciliation,
          disabled,
        },
      });
      return;
    }
    if (tokenlessAttachmentKeys.length > 0) {
      void frontendLog("DEBUG", "inline_attachment_token_pending", "输入框附件尚未写入对应 token", {
        data: {
          tokenlessAttachmentKeys,
          attachmentKeys,
          tokenKeys,
          suspendAttachmentReconciliation,
          disabled,
        },
      });
    }
  }, [disabled, reconciliation, suspendAttachmentReconciliation]);

  const rememberCaret = useCallback(() => {
    const editor = editorRef.current;
    const selection = window.getSelection();
    if (!editor || !selection || selection.rangeCount === 0 || !selection.anchorNode || !editor.contains(selection.anchorNode)) return;

    const range = selection.getRangeAt(0).cloneRange();
    range.selectNodeContents(editor);
    range.setEnd(selection.anchorNode, selection.anchorOffset);
    caretOffset.current = serializeNode(range.cloneContents()).length;
  }, []);

  const restoreCaret = useCallback((offset: number) => {
    const editor = editorRef.current;
    if (!editor) return;
    let remaining = Math.max(0, offset);
    const range = document.createRange();
    const visit = (node: Node): boolean => {
      if (node.nodeType === Node.TEXT_NODE) {
        const length = node.textContent?.length ?? 0;
        if (remaining <= length) {
          range.setStart(node, remaining);
          range.collapse(true);
          return true;
        }
        remaining -= length;
        return false;
      }
      if (node.nodeType === Node.ELEMENT_NODE && (node as HTMLElement).dataset.attachmentToken === "true") {
        const length = serializeNode(node).length;
        if (remaining <= length) {
          range.setStartAfter(node);
          range.collapse(true);
          return true;
        }
        remaining -= length;
        return false;
      }
      for (const child of [...node.childNodes]) {
        if (visit(child)) return true;
      }
      return false;
    };
    if (!visit(editor)) {
      range.selectNodeContents(editor);
      range.collapse(false);
    }
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    editor.focus();
  }, []);

  const insertAttachmentsAtCaret = useCallback((newAttachments: readonly InlineFileAttachment[]) => {
    if (disabled || newAttachments.length === 0) return;
    let nextValue = currentValue.current;
    let offset = caretOffset.current ?? nextValue.length;
    for (const attachment of newAttachments) {
      const kind = attachment.kind ?? "file";
      const tokenId = attachment.tokenId ?? attachment.id;
      const token = `[[cosir-${kind}:${tokenId}]]`;
      nextValue = `${nextValue.slice(0, offset)}${token}${nextValue.slice(offset)}`;
      offset += token.length;
    }
    caretOffset.current = offset;
    onChange(nextValue);
    requestAnimationFrame(() => restoreCaret(offset));
  }, [disabled, onChange, restoreCaret]);

  useEffect(() => insertion.register(insertAttachmentsAtCaret), [insertAttachmentsAtCaret, insertion]);

  const handleExternalFiles = useCallback(async (files: readonly File[]) => {
    if (disabled || files.length === 0) return;
    if (!onExternalFiles) {
      void frontendLog("INFO", "inline_attachment_external_files_ignored", "输入框未装配外部附件处理器，已忽略文件输入", {
        data: { fileCount: files.length },
      });
      return;
    }
    try {
      const inlineAttachments = await onExternalFiles(files);
      insertAttachmentsAtCaret(inlineAttachments);
    } catch (error) {
      void frontendLog("ERROR", "inline_attachment_external_files_failed", "处理粘贴或拖拽附件失败", {
        data: { fileCount: files.length },
        error,
      });
    }
  }, [disabled, insertAttachmentsAtCaret, onExternalFiles]);

  useEffect(() => {
    if (autoFocus) editorRef.current?.focus();
  }, [autoFocus]);

  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) return;
    const currentValue = serializeEditor(editor);
    const signature = `${value}\u0000${attachmentSignature}`;
    if (currentValue !== value || lastMarkupSignature.current !== signature) {
      // 草稿写入尚未完成时，未匹配 token 按「同步中」呈现，避免把同步延迟误报为失效。
      editor.innerHTML = renderInlineAttachmentHtml(value, attachments, {
        unmatchedAsPending: suspendAttachmentReconciliation,
      });
      lastMarkupSignature.current = signature;
    }
  }, [attachmentSignature, attachments, suspendAttachmentReconciliation, value]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (disabled) return;
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
      className={cn("min-w-0 max-h-48 overflow-y-auto", className)}
      onInput={(event) => {
        const removedTags = removeUnsupportedEmbeddedContent(event.currentTarget);
        if (removedTags.length > 0) {
          void frontendLog("WARNING", "inline_attachment_embedded_content_removed", "输入框移除了未受支持的内嵌媒体节点", {
            data: {
              removedTags,
              attachmentCount: attachments.length,
            },
          });
        }
        const nextValue = serializeEditor(event.currentTarget);
        currentValue.current = nextValue;
        rememberCaret();
        lastMarkupSignature.current = `${nextValue}\u0000${attachmentSignature}`;
        onChange(nextValue);
      }}
      onPaste={(event) => {
        const files = [...event.clipboardData.files];
        if (files.length > 0) {
          event.preventDefault();
          void handleExternalFiles(files);
          return;
        }
        // Let normal text paste proceed. A following input event removes any
        // browser-inserted media before it can become persistent composer DOM.
        requestAnimationFrame(() => {
          const editor = editorRef.current;
          if (!editor) return;
          const removedTags = removeUnsupportedEmbeddedContent(editor);
          if (removedTags.length === 0) return;
          const nextValue = serializeEditor(editor);
          currentValue.current = nextValue;
          lastMarkupSignature.current = `${nextValue}\u0000${attachmentSignature}`;
          onChange(nextValue);
          rememberCaret();
        });
      }}
      onDrop={(event) => {
        if (event.defaultPrevented || event.dataTransfer.files.length === 0) return;
        event.preventDefault();
        void handleExternalFiles([...event.dataTransfer.files]);
      }}
      onKeyDown={handleKeyDown}
      onKeyUp={rememberCaret}
      onMouseUp={rememberCaret}
      onFocus={rememberCaret}
      onClick={(event) => {
        if (disabled) return;
        const target = event.target as HTMLElement;
        const removeButton = target.closest<HTMLElement>("[data-file-remove='true']");
        if (!removeButton) return;
        if (!onRemoveAttachment) {
          void frontendLog("WARNING", "inline_attachment_remove_unavailable", "点击移除附件，但当前输入框没有移除回调", {
            data: { disabled },
          });
          return;
        }
        const token = removeButton.closest<HTMLElement>("[data-attachment-token='true']");
        const rawKind = token?.dataset.attachmentKind;
        const kind = rawKind === "file" || rawKind === "image" ? rawKind : undefined;
        const attachmentId = token?.dataset.attachmentId;
        const tokenId = token?.dataset.tokenId;
        if (!kind || !tokenId || !attachmentId) {
          void frontendLog("WARNING", "inline_attachment_remove_ignored", "点击移除附件，但 token 缺少定位信息（失效 token 不带附件 id，无法移除）", {
            data: {
              missingKind: kind === undefined,
              missingTokenId: tokenId === undefined,
              missingAttachmentId: attachmentId === undefined,
              tokenId: tokenId ?? null,
            },
          });
          return;
        }
        event.preventDefault();
        const nextValue = removePlaceholder(value, kind, tokenId);
        void frontendLog("INFO", "inline_attachment_remove_requested", "请求移除内联附件 token", {
          data: {
            kind,
            tokenId,
            attachmentId,
            textLengthBefore: value.length,
            textLengthAfter: nextValue.length,
            remainingTokenKeys: textAttachmentTokenKeys(nextValue),
          },
        });
        onChange(nextValue);
        onRemoveAttachment(attachmentId);
      }}
      onBlur={() => {
        // 不回写：React 提交前 DOM 可能落后于 composer 状态（例如刚点 × 移除 token 的同一帧），
        // 反序列化旧 DOM 会把已删除的 token 写回。文本同步只由 onInput 驱动。
        rememberCaret();
      }}
    />
  );
}
