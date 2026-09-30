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

/**
 * 三种 composer 输入场景共用的编辑区视觉契约。
 *
 * 高度、内边距、字体和焦点轮廓集中在这里，调用方只补充场景级布局，不再分别维护
 * 新建会话、主会话和编辑消息的输入框尺寸。
 */
export const INLINE_ATTACHMENT_INPUT_CLASS_NAME =
  "text-foreground min-h-20 w-full bg-transparent px-2.5 py-2 text-base leading-6 outline-none focus-visible:outline-none data-[disabled=true]:cursor-not-allowed data-[disabled=true]:opacity-60";

type InlineComposerInsertionContextValue = {
  register: (handler: (attachments: readonly InlineFileAttachment[]) => void) => () => void;
  insert: (attachments: readonly InlineFileAttachment[]) => void;
  registerTextInsertion: (handler: (text: string) => void) => () => void;
  insertText: (text: string) => void;
};

const InlineComposerInsertionContext = createContext<InlineComposerInsertionContextValue | null>(null);

/**
 * Connect attachment pickers and text snippet controls to the contenteditable that owns token order.
 *
 * Controls insert through registered handlers so the editor can preserve its
 * logical caret offset, attachment-token order, and controlled text value.
 * This deliberately keeps insertion event-driven; attachment counts are not
 * used to infer text mutations.
 */
/**
 * Own insertion callbacks for one composer surface.
 *
 * @param children - The editor and controls allowed to exchange attachment or text insertions.
 * @returns A React provider element; it owns callbacks only for its mounted subtree.
 */
export function InlineComposerInsertionProvider({ children }: PropsWithChildren) {
  const handlerRef = useRef<((attachments: readonly InlineFileAttachment[]) => void) | null>(null);
  const textHandlerRef = useRef<((text: string) => void) | null>(null);
  const register = useCallback((handler: (attachments: readonly InlineFileAttachment[]) => void) => {
    handlerRef.current = handler;
    return () => {
      if (handlerRef.current === handler) handlerRef.current = null;
    };
  }, []);
  const insert = useCallback((attachments: readonly InlineFileAttachment[]) => {
    handlerRef.current?.(attachments);
  }, []);
  const registerTextInsertion = useCallback((handler: (text: string) => void) => {
    textHandlerRef.current = handler;
    return () => {
      if (textHandlerRef.current === handler) textHandlerRef.current = null;
    };
  }, []);
  const insertText = useCallback((text: string) => {
    textHandlerRef.current?.(text);
  }, []);
  const contextValue = useMemo(
    () => ({ register, insert, registerTextInsertion, insertText }),
    [insert, insertText, register, registerTextInsertion],
  );

  return (
    <InlineComposerInsertionContext.Provider value={contextValue}>
      {children}
    </InlineComposerInsertionContext.Provider>
  );
}

/**
 * Read the insertion bridge for the nearest composer surface.
 *
 * @returns Registration and dispatch functions for text and attachment insertion.
 * @throws Error when called outside `InlineComposerInsertionProvider`; insertion controls require one editor owner.
 */
export function useInlineComposerInsertion(): InlineComposerInsertionContextValue {
  const context = useContext(InlineComposerInsertionContext);
  if (!context) {
    throw new Error("InlineComposerInsertionProvider is required for composer insertion controls");
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

/**
 * 可用于判断回车提交的最小键盘事件契约。
 *
 * 该类型只描述输入边界需要的字段，不把具体编辑器、Assistant UI 或浏览器对象泄漏到
 * 判定函数中，便于用纯单元测试覆盖输入法和普通回车的分支。
 */
export type ComposerEnterKeyEvent = {
  key: string;
  shiftKey: boolean;
  defaultPrevented: boolean;
  nativeEvent: {
    isComposing: boolean;
    keyCode: number;
  };
};

/**
 * 判断一次回车键事件是否可以提交消息。
 *
 * 组合输入由三层信号共同保护：组件维护的 composition 生命周期、浏览器标准
 * ``isComposing`` 标志，以及当前 WebView 在 IME 确认候选词时使用的 229 keyCode。229
 * 只在本输入边界做事件归一化，不参与业务发送逻辑；否则中文输入法确认候选词会被误当成发送。
 */
export function shouldSubmitOnEnter(
  event: ComposerEnterKeyEvent,
  isComposing: boolean,
): boolean {
  return (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.defaultPrevented &&
    !isComposing &&
    !event.nativeEvent.isComposing &&
    event.nativeEvent.keyCode !== 229
  );
}

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

/**
 * 在逻辑文本的指定偏移处插入附件 token。
 *
 * 负责什么：按调用方提供的顺序，把附件转换为内联 token 并返回新文本与新光标位置。
 * 不负责什么：不修改附件对象、不写入 composer，也不判断附件是否已完成上传。
 *
 * 参数:
 *     value: 当前受控文本，可能已经包含普通文本和已有 token。
 *     offset: 逻辑文本中的插入偏移；超出范围时收敛到文本边界。
 *     attachments: 要插入的附件视图，`kind` 与 `tokenId` 决定 token 身份。
 *
 * 返回:
 *     插入后的文本以及位于最后一个新 token 之后的逻辑光标偏移。
 *
 * 异常/副作用:
 *     无；纯函数。
 */
export function insertInlineAttachmentsAtOffset(
  value: string,
  offset: number,
  attachments: readonly InlineFileAttachment[],
): { value: string; caretOffset: number } {
  let nextValue = value;
  let nextOffset = Math.min(Math.max(0, offset), value.length);
  for (const attachment of attachments) {
    const kind = attachment.kind ?? "file";
    const tokenId = attachment.tokenId ?? attachment.id;
    const token = `[[cosir-${kind}:${tokenId}]]`;
    nextValue = `${nextValue.slice(0, nextOffset)}${token}${nextValue.slice(nextOffset)}`;
    nextOffset += token.length;
  }
  return { value: nextValue, caretOffset: nextOffset };
}

/**
 * 判断异步操作排队的光标恢复是否仍属于当前编辑代际。
 *
 * 负责什么：阻止旧的 requestAnimationFrame 在用户已经继续编辑后抢回光标。
 * 不负责什么：不执行恢复、不管理 revision 的递增；revision 由输入事件和程序化写入边界维护。
 *
 * 参数:
 *     currentRevision: 当前编辑器 revision。
 *     scheduledRevision: 排队恢复光标时记录的 revision。
 *
 * 返回:
 *     仅当两者相等时返回 true。
 *
 * 异常/副作用:
 *     无；纯函数。
 */
export function isCurrentEditorRevision(currentRevision: number, scheduledRevision: number): boolean {
  return currentRevision === scheduledRevision;
}

/**
 * 判断受控父组件的旧 value 是否应暂时让位于编辑器本地值。
 *
 * 负责什么：覆盖 React 受控更新尚未回传、但 DOM 已经完成用户输入或程序化插入的短窗口。
 * 不负责什么：不修改 ref、不决定外部 value 是否最终接受；外部 value 发生真实变化时由调用方
 * 清理 pending 值并执行同步。
 *
 * 返回:
 *     true 表示当前 prop 仍是旧值，应保留编辑器本地 DOM。
 *
 * 异常/副作用:
 *     无；纯函数。
 */
export function shouldPreservePendingEditorValue(
  propValue: string,
  lastPropValue: string,
  pendingInternalValue: string | null,
): boolean {
  return pendingInternalValue !== null
    && propValue === lastPropValue
    && propValue !== pendingInternalValue;
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
  const isComposingRef = useRef(false);
  const lastMarkupSignature = useRef("");
  const caretOffset = useRef<number | null>(null);
  const currentValue = useRef(value);
  const lastPropValue = useRef(value);
  const pendingInternalValue = useRef<string | null>(null);
  const editorRevision = useRef(0);
  const disabledRef = useRef(disabled);
  const insertion = useInlineComposerInsertion();
  const attachmentSignature = JSON.stringify(attachments.map((attachment) => ({
    id: attachment.id,
    name: attachment.name,
    kind: attachment.kind ?? "file",
    tokenId: attachment.tokenId ?? attachment.id,
  })));

  // 内部插入先更新 ref，再通知受控父组件。父组件可能在同一轮渲染中暂时仍返回旧 value，
  // 此时不能用旧 prop 覆盖刚合并完成的 token；只有观察到 prop 真正变化时才同步外部值。
  if (value !== lastPropValue.current) {
    currentValue.current = value;
    lastPropValue.current = value;
    pendingInternalValue.current = null;
  }
  disabledRef.current = disabled;

  // 文本 token 与附件对象是两条独立写入路径，任一侧滞后都会影响内联胶囊的呈现。这里
  // 统一算一次对账结果，供渲染（区分「同步中」与「已失效」）与诊断日志共用。
  // 依赖只使用附件内容签名和文本值；签名覆盖真正参与匹配的 kind、tokenId、id、name，
  // 避免仅因父组件创建了新数组就重复计算。
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
  }, [attachmentSignature, value]);

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
    editorRevision.current += 1;
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
        if (remaining === 0) {
          range.setStartBefore(node);
          range.collapse(true);
          return true;
        }
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
    if (disabledRef.current || newAttachments.length === 0) return;
    const result = insertInlineAttachmentsAtOffset(
      currentValue.current,
      caretOffset.current ?? currentValue.current.length,
      newAttachments,
    );
    currentValue.current = result.value;
    caretOffset.current = result.caretOffset;
    pendingInternalValue.current = result.value;
    lastMarkupSignature.current = `${result.value}\u0000${attachmentSignature}`;
    const editor = editorRef.current;
    if (editor) {
      editor.innerHTML = renderInlineAttachmentHtml(result.value, [...attachments, ...newAttachments], {
        unmatchedAsPending: true,
      });
    }
    editorRevision.current += 1;
    const scheduledRevision = editorRevision.current;
    onChange(result.value);
    requestAnimationFrame(() => {
      if (!isCurrentEditorRevision(editorRevision.current, scheduledRevision)) return;
      restoreCaret(result.caretOffset);
    });
  }, [attachmentSignature, attachments, onChange, restoreCaret]);

  const insertTextAtCaret = useCallback((text: string) => {
    if (disabledRef.current || text.length === 0) return;
    const current = currentValue.current;
    const offset = Math.min(Math.max(0, caretOffset.current ?? current.length), current.length);
    const nextValue = `${current.slice(0, offset)}${text}${current.slice(offset)}`;
    const nextOffset = offset + text.length;
    currentValue.current = nextValue;
    caretOffset.current = nextOffset;
    pendingInternalValue.current = nextValue;
    lastMarkupSignature.current = `${nextValue}\u0000${attachmentSignature}`;
    const editor = editorRef.current;
    if (editor) {
      editor.innerHTML = renderInlineAttachmentHtml(nextValue, attachments, {
        unmatchedAsPending: true,
      });
    }
    editorRevision.current += 1;
    const scheduledRevision = editorRevision.current;
    onChange(nextValue);
    requestAnimationFrame(() => {
      if (!isCurrentEditorRevision(editorRevision.current, scheduledRevision)) return;
      restoreCaret(nextOffset);
    });
  }, [attachmentSignature, attachments, onChange, restoreCaret]);

  useEffect(() => insertion.register(insertAttachmentsAtCaret), [insertAttachmentsAtCaret, insertion]);
  useEffect(
    () => insertion.registerTextInsertion(insertTextAtCaret),
    [insertTextAtCaret, insertion],
  );

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
    // 内部插入已经立即更新 DOM，但受控父组件可能尚未提交新 value。此时跳过旧 prop
    // 的回写，等待父组件确认内部值；否则异步附件 token 会被一帧内的旧草稿覆盖。
    if (shouldPreservePendingEditorValue(value, lastPropValue.current, pendingInternalValue.current)) {
      return;
    }
    if (currentValue !== value || lastMarkupSignature.current !== signature) {
      // 草稿写入尚未完成时，未匹配 token 按「同步中」呈现，避免把同步延迟误报为失效。
      editorRevision.current += 1;
      editor.innerHTML = renderInlineAttachmentHtml(value, attachments, {
        unmatchedAsPending: suspendAttachmentReconciliation,
      });
      lastMarkupSignature.current = signature;
    }
  }, [attachmentSignature, attachments, suspendAttachmentReconciliation, value]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (disabled) return;
    if (!shouldSubmitOnEnter(event, isComposingRef.current)) return;
    event.preventDefault();
    onSubmit();
  };

  return (
    <div
      ref={editorRef}
      contentEditable={!disabled}
      suppressContentEditableWarning
      role="textbox"
      aria-label={ariaLabel}
      aria-multiline="true"
      aria-disabled={disabled}
      data-placeholder={placeholder}
      data-empty={value.trim().length === 0 ? "true" : "false"}
      data-disabled={disabled ? "true" : "false"}
      className={cn("min-w-0 min-h-20 max-h-48 w-full overflow-y-auto", className)}
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
        pendingInternalValue.current = nextValue;
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
          pendingInternalValue.current = nextValue;
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
      onCompositionStart={() => {
        isComposingRef.current = true;
      }}
      onCompositionEnd={() => {
        isComposingRef.current = false;
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
        const currentText = currentValue.current;
        const nextValue = removePlaceholder(currentText, kind, tokenId);
        token?.remove();
        currentValue.current = nextValue;
        pendingInternalValue.current = nextValue;
        editorRevision.current += 1;
        lastMarkupSignature.current = `${nextValue}\u0000${attachmentSignature}`;
        void frontendLog("INFO", "inline_attachment_remove_requested", "请求移除内联附件 token", {
          data: {
            kind,
            tokenId,
            attachmentId,
            textLengthBefore: currentText.length,
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
