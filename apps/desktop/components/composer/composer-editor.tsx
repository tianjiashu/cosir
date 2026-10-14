"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import { StarterKit } from "@tiptap/starter-kit";
import { AttachmentToken } from "@/components/composer/extensions/attachment-token";
import { SkillToken } from "@/components/composer/extensions/skill-token";
import { ComposerEditorViewProvider } from "@/components/composer/composer-editor-context";
import { resolveComposerInsertionPosition, shouldSubmitOnEnter } from "@/components/composer/composer-editor-policy";
import {
  insertionCommandContent,
  parseComposerDocument,
  reconcileComposerAttachments,
  serializeComposerDocument,
  shouldPreservePendingEditorValue,
  type InlineComposerAttachment,
} from "@/components/composer/composer-document";
import { useComposerFeedback } from "@/components/composer/composer-feedback";
import { summarizeComposerFileProblems, type ComposerExternalFileResult } from "@/components/composer/composer-external-files";
import { useComposerInsertion } from "@/components/composer/composer-insertion-context";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";

export const COMPOSER_EDITOR_CLASS_NAME =
  "text-foreground min-h-20 w-full bg-transparent px-2.5 py-2 text-base leading-6 outline-none focus-visible:outline-none data-[disabled=true]:cursor-not-allowed data-[disabled=true]:opacity-60";

type ComposerEditorProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  attachments: readonly InlineComposerAttachment[];
  onExternalFiles?: (files: readonly File[]) => ComposerExternalFileResult<InlineComposerAttachment> | Promise<ComposerExternalFileResult<InlineComposerAttachment>>;
  onRemoveAttachment?: (id: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  disabled?: boolean;
  suspendAttachmentReconciliation?: boolean;
  className?: string;
  "aria-label"?: string;
};

const composerExtensions = [
  StarterKit.configure({
    blockquote: false,
    bold: false,
    bulletList: false,
    code: false,
    codeBlock: false,
    dropcursor: false,
    gapcursor: false,
    heading: false,
    horizontalRule: false,
    italic: false,
    link: false,
    listItem: false,
    listKeymap: false,
    orderedList: false,
    strike: false,
    trailingNode: false,
    underline: false,
  }),
  AttachmentToken,
  SkillToken,
];

/**
 * 以 Tiptap 管理输入文档、选区和原子 token；对外仍只暴露纯文本及现有附件回调。
 * 后端事实、发送事务和本机进程边界均不由编辑器持有。
 */
export function ComposerEditor({
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
}: ComposerEditorProps) {
  const insertion = useComposerInsertion();
  const feedback = useComposerFeedback();
  const isComposingRef = useRef(false);
  const hasCapturedSelectionRef = useRef(false);
  const lastPropValueRef = useRef(value);
  const pendingInternalValueRef = useRef<string | null>(null);
  const propsRef = useRef({ onChange, onSubmit, onExternalFiles, disabled, attachments });
  const [currentValue, setCurrentValue] = useState(value);
  propsRef.current = { onChange, onSubmit, onExternalFiles, disabled, attachments };

  if (value !== lastPropValueRef.current) {
    lastPropValueRef.current = value;
    pendingInternalValueRef.current = null;
  }

  const editor = useEditor({
    immediatelyRender: false,
    extensions: composerExtensions,
    content: parseComposerDocument(value),
    editable: !disabled,
    onUpdate: ({ editor: updatedEditor }) => {
      const nextValue = serializeComposerDocument(updatedEditor.getJSON());
      pendingInternalValueRef.current = nextValue;
      setCurrentValue(nextValue);
      propsRef.current.onChange(nextValue);
    },
    editorProps: {
      attributes: {
        role: "textbox",
        "aria-multiline": "true",
      },
      handleKeyDown: (_view, event) => {
        const current = propsRef.current;
        const enterEvent = {
          key: event.key,
          shiftKey: event.shiftKey,
          defaultPrevented: event.defaultPrevented,
          nativeEvent: { isComposing: event.isComposing, keyCode: event.keyCode },
        };
        if (current.disabled || !shouldSubmitOnEnter(enterEvent, isComposingRef.current)) return false;
        event.preventDefault();
        current.onSubmit();
        return true;
      },
      handlePaste: (_view, event) => {
        const files = [...(event.clipboardData?.files ?? [])];
        if (files.length === 0) return false;
        event.preventDefault();
        const text = event.clipboardData?.getData("text/plain") ?? "";
        if (text) {
          editor?.chain().focus().insertContent(insertionCommandContent({ type: "insert-text", text })).run();
          hasCapturedSelectionRef.current = true;
        }
        void handleExternalFiles(files);
        return true;
      },
      handleDrop: (_view, event) => {
        const files = [...(event.dataTransfer?.files ?? [])];
        if (files.length === 0) return false;
        event.preventDefault();
        const text = event.dataTransfer?.getData("text/plain") ?? "";
        if (text) {
          editor?.chain().focus().insertContent(insertionCommandContent({ type: "insert-text", text })).run();
          hasCapturedSelectionRef.current = true;
        }
        void handleExternalFiles(files);
        return true;
      },
    },
  });

  const attachmentSignature = JSON.stringify(attachments.map((attachment) => ({
    id: attachment.id,
    name: attachment.name,
    kind: attachment.kind ?? "file",
    tokenId: attachment.tokenId ?? attachment.id,
  })));
  const reconciliation = useMemo(
    () => reconcileComposerAttachments(value, attachments),
    [attachmentSignature, value],
  );
  const reconciliationSignatureRef = useRef("");

  useEffect(() => {
    if (!editor) return;
    if (shouldPreservePendingEditorValue(value, lastPropValueRef.current, pendingInternalValueRef.current)) return;
    if (value === pendingInternalValueRef.current) pendingInternalValueRef.current = null;
    if (serializeComposerDocument(editor.getJSON()) !== value) {
      editor.commands.setContent(parseComposerDocument(value), { emitUpdate: false });
    }
    setCurrentValue(value);
  }, [editor, value]);

  useEffect(() => {
    if (!editor) return;
    editor.setEditable(!disabled);
    const dom = editor.view.dom;
    dom.className = cn("min-w-0 min-h-20 max-h-48 w-full overflow-y-auto whitespace-pre-wrap break-words", className);
    dom.setAttribute("role", "textbox");
    dom.setAttribute("aria-multiline", "true");
    dom.setAttribute("aria-disabled", String(disabled));
    dom.setAttribute("data-disabled", String(disabled));
    dom.setAttribute("data-empty", currentValue.trim().length === 0 ? "true" : "false");
    if (placeholder) dom.setAttribute("aria-placeholder", placeholder);
    else dom.removeAttribute("aria-placeholder");
    if (ariaLabel) dom.setAttribute("aria-label", ariaLabel);
    else dom.removeAttribute("aria-label");
    if (placeholder) dom.setAttribute("data-placeholder", placeholder);
    else dom.removeAttribute("data-placeholder");
  }, [ariaLabel, className, currentValue, disabled, editor, placeholder]);

  useEffect(() => {
    const { attachmentKeys, tokenKeys, unmatchedTokenKeys, tokenlessAttachmentKeys } = reconciliation;
    const signature = `${unmatchedTokenKeys.join(",")}|${tokenlessAttachmentKeys.join(",")}|${suspendAttachmentReconciliation}|${disabled}`;
    if (signature === reconciliationSignatureRef.current) return;
    reconciliationSignatureRef.current = signature;

    if (unmatchedTokenKeys.length > 0) {
      const stage = suspendAttachmentReconciliation ? "同步中" : "已失效";
      void frontendLog("WARNING", "inline_attachment_token_unmatched", `输入框存在没有附件对象的 token，显示为「附件${stage}」`, {
        data: { unmatchedTokenKeys, attachmentKeys, tokenKeys, suspendAttachmentReconciliation, disabled },
      });
      return;
    }
    if (tokenlessAttachmentKeys.length > 0) {
      void frontendLog("DEBUG", "inline_attachment_token_pending", "输入框附件尚未写入对应 token", {
        data: { tokenlessAttachmentKeys, attachmentKeys, tokenKeys, suspendAttachmentReconciliation, disabled },
      });
    }
  }, [disabled, reconciliation, suspendAttachmentReconciliation]);

  const insertCommand = useCallback((command: Parameters<typeof insertionCommandContent>[0]) => {
    if (!editor || !editor.isEditable) return;
    const content = insertionCommandContent(command);
    if (content.length === 0) return;
    const position = resolveComposerInsertionPosition(
      hasCapturedSelectionRef.current,
      editor.state.selection.anchor,
      editor.state.doc.content.size,
    );
    editor.chain().focus().setTextSelection(position).insertContent(content).run();
    hasCapturedSelectionRef.current = true;
  }, [editor]);

  useEffect(() => insertion.register(insertCommand), [insertion, insertCommand]);

  const handleExternalFiles = useCallback(async (files: readonly File[]) => {
    const current = propsRef.current;
    if (current.disabled || files.length === 0) return;
    if (!current.onExternalFiles) {
      void frontendLog("INFO", "inline_attachment_external_files_ignored", "输入框未装配外部附件处理器，已忽略文件输入", {
        data: { fileCount: files.length },
      });
      feedback.show("此输入框暂不支持添加附件。");
      return;
    }
    try {
      const result = await current.onExternalFiles(files);
      if (propsRef.current.disabled) return;
      if (result.attachments.length > 0) {
        insertion.dispatch({ type: "insert-attachments", attachments: result.attachments });
      }
      const rejectedFiles = result.problems.filter((problem) => problem.reason === "unsupported-image");
      if (rejectedFiles.length > 0) {
        void frontendLog("WARNING", "composer_external_image_rejected", "已跳过不支持的图片附件", {
          data: { count: rejectedFiles.length, names: rejectedFiles.map((problem) => problem.name) },
        });
      }
      const problemMessage = summarizeComposerFileProblems(result.problems);
      if (problemMessage) feedback.show(problemMessage);
      else feedback.clear();
    } catch (error) {
      feedback.show(safeFrontendErrorMessage(error, "添加附件失败，请重试"));
      void frontendLog("ERROR", "inline_attachment_external_files_failed", "处理粘贴或拖拽附件失败", {
        data: { fileCount: files.length },
        error,
      });
    }
  }, [feedback, insertion]);

  const captureUserSelection = (target: EventTarget | null) => {
    if (!editor || !(target instanceof Node) || !editor.view.dom.contains(target)) return;
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || !selection.anchorNode) return;
    if (!editor.view.dom.contains(selection.anchorNode)) return;
    hasCapturedSelectionRef.current = true;
  };

  useEffect(() => {
    if (autoFocus) editor?.commands.focus();
  }, [autoFocus, editor]);

  const attachmentView = useMemo(() => ({
    attachments,
    disabled,
    unmatchedAsPending: suspendAttachmentReconciliation,
    onRemoveAttachment,
  }), [attachments, disabled, onRemoveAttachment, suspendAttachmentReconciliation]);

  return (
    <ComposerEditorViewProvider value={attachmentView}>
      <EditorContent
        editor={editor}
        onFocus={(event) => {
          captureUserSelection(event.target);
        }}
        onMouseUp={(event) => captureUserSelection(event.target)}
        onKeyUp={(event) => captureUserSelection(event.target)}
        onCompositionStart={() => { isComposingRef.current = true; }}
        onCompositionEnd={() => { isComposingRef.current = false; }}
      />
    </ComposerEditorViewProvider>
  );
}
