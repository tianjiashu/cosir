"use client";

import {
  ReferenceTargetScope,
  useReferenceBridge,
} from "@/components/workspace-references/reference-bridge";
import {
  decodeReferences,
  referenceIdentity,
} from "@/lib/workspace-references/codec";
import { WorkspaceReferenceNode } from "./extensions/workspace-reference";
import { ReferenceMention } from "@/components/workspace-references/reference-mention";

import { useCallback, useContext, useEffect, useMemo, useRef } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import { StarterKit } from "@tiptap/starter-kit";
import { Placeholder } from "@tiptap/extension-placeholder";
import { AttachmentToken } from "@/components/composer/extensions/attachment-token";
import { SkillToken } from "@/components/composer/extensions/skill-token";
import { ComposerEditorViewProvider } from "@/components/composer/composer-editor-context";
import {
  resolveComposerInsertionPosition,
  shouldSubmitOnEnter,
} from "@/components/composer/composer-editor-policy";
import {
  insertionCommandContent,
  parseComposerDocument,
  reconcileComposerAttachments,
  serializeComposerDocument,
  shouldPreservePendingEditorValue,
  type InlineComposerAttachment,
} from "@/components/composer/composer-document";
import { useComposerFeedback } from "@/components/composer/composer-feedback";
import {
  summarizeComposerFileProblems,
  type ComposerExternalFileResult,
} from "@/components/composer/composer-external-files";
import { useComposerInsertion } from "@/components/composer/composer-insertion-context";
import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { notify } from "@/lib/notify";

/** 新对话页与任务内输入框共用的占位文案：同一种语气（描述要做的事 + 一个具体例子）。 */
export const COMPOSER_PLACEHOLDER = "描述要做的事，例如：帮我查找登录相关代码…";

export const COMPOSER_EDITOR_CLASS_NAME =
  "text-foreground min-h-20 w-full bg-transparent px-2.5 py-2 text-base leading-6 outline-none focus-visible:outline-none data-[disabled=true]:cursor-not-allowed data-[disabled=true]:opacity-60";

type ComposerEditorProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  attachments: readonly InlineComposerAttachment[];
  onExternalFiles?: (
    files: readonly File[],
  ) =>
    | ComposerExternalFileResult<InlineComposerAttachment>
    | Promise<ComposerExternalFileResult<InlineComposerAttachment>>;
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
  WorkspaceReferenceNode,
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
  const referenceBridge = useReferenceBridge();
  const explicitTarget = useContext(ReferenceTargetScope);
  const target = explicitTarget ?? referenceBridge?.target;
  const feedback = useComposerFeedback();
  const showFeedback = feedback.show;
  const isComposingRef = useRef(false);
  const hasCapturedSelectionRef = useRef(false);
  const lastPropValueRef = useRef(value);
  const pendingInternalValueRef = useRef<string | null>(null);
  const propsRef = useRef({
    onChange,
    onSubmit,
    onExternalFiles,
    disabled,
    attachments,
    placeholder,
  });
  propsRef.current = {
    onChange,
    onSubmit,
    onExternalFiles,
    disabled,
    attachments,
    placeholder,
  };

  const extensions = useMemo(
    () => [
      ...composerExtensions,
      Placeholder.configure({
        placeholder: () => propsRef.current.placeholder ?? "",
        showOnlyWhenEditable: false,
      }),
    ],
    [],
  );

  if (value !== lastPropValueRef.current) {
    lastPropValueRef.current = value;
    pendingInternalValueRef.current = null;
  }

  const editor = useEditor({
    immediatelyRender: false,
    extensions,
    content: parseComposerDocument(value),
    editable: !disabled,
    onUpdate: ({ editor: updatedEditor }) => {
      const nextValue = serializeComposerDocument(updatedEditor.getJSON());
      pendingInternalValueRef.current = nextValue;
      propsRef.current.onChange(nextValue);
    },
    editorProps: {
      // 让 useEditor 持续管理属性，保留 ProseMirror 类名且不被后续渲染覆盖。
      attributes: {
        class: cn(
          "cosir-composer-editor min-w-0 min-h-20 max-h-48 w-full overflow-y-auto whitespace-pre-wrap break-words",
          className,
        ),
        role: "textbox",
        "aria-multiline": "true",
        "aria-disabled": String(disabled),
        "data-disabled": String(disabled),
        ...(placeholder ? { "aria-placeholder": placeholder } : {}),
        ...(ariaLabel ? { "aria-label": ariaLabel } : {}),
      },
      handleKeyDown: (_view, event) => {
        const current = propsRef.current;
        const enterEvent = {
          key: event.key,
          shiftKey: event.shiftKey,
          defaultPrevented: event.defaultPrevented,
          nativeEvent: {
            isComposing: event.isComposing,
            keyCode: event.keyCode,
          },
        };
        if (
          current.disabled ||
          !shouldSubmitOnEnter(enterEvent, isComposingRef.current)
        )
          return false;
        event.preventDefault();
        current.onSubmit();
        return true;
      },
      clipboardTextSerializer: (slice) =>
        serializeComposerDocument({
          type: "doc",
          content: slice.content.toJSON(),
        }),
      handlePaste: (_view, event) => {
        const files = [...(event.clipboardData?.files ?? [])];
        if (files.length === 0) {
          const text = event.clipboardData?.getData("text/plain") ?? "";
          if (!decodeReferences(text).some((part) => part.type === "reference"))
            return false;
          event.preventDefault();
          if (!propsRef.current.disabled)
            editor?.commands.insertContent(
              parseComposerDocument(text).content ?? [],
            );
          return true;
        }
        event.preventDefault();
        if (rejectFilesWhileDisabled("paste", files.length)) return true;
        const text = event.clipboardData?.getData("text/plain") ?? "";
        if (text) {
          editor
            ?.chain()
            .focus()
            .insertContent(
              insertionCommandContent({ type: "insert-text", text }),
            )
            .run();
          hasCapturedSelectionRef.current = true;
        }
        void handleExternalFiles(files);
        return true;
      },
      handleDrop: (_view, event) => {
        const files = [...(event.dataTransfer?.files ?? [])];
        if (files.length === 0) return false;
        event.preventDefault();
        if (rejectFilesWhileDisabled("drop", files.length)) return true;
        const text = event.dataTransfer?.getData("text/plain") ?? "";
        if (text) {
          editor
            ?.chain()
            .focus()
            .insertContent(
              insertionCommandContent({ type: "insert-text", text }),
            )
            .run();
          hasCapturedSelectionRef.current = true;
        }
        void handleExternalFiles(files);
        return true;
      },
    },
  });

  const attachmentSignature = JSON.stringify(
    attachments.map((attachment) => ({
      id: attachment.id,
      name: attachment.name,
      kind: attachment.kind ?? "file",
      tokenId: attachment.tokenId ?? attachment.id,
    })),
  );
  const reconciliation = useMemo(
    () => reconcileComposerAttachments(value, attachments),
    [attachmentSignature, value],
  );
  const reconciliationSignatureRef = useRef("");

  useEffect(() => {
    if (!editor) return;
    if (
      shouldPreservePendingEditorValue(
        value,
        lastPropValueRef.current,
        pendingInternalValueRef.current,
      )
    )
      return;
    if (value === pendingInternalValueRef.current)
      pendingInternalValueRef.current = null;
    if (serializeComposerDocument(editor.getJSON()) !== value) {
      editor.commands.setContent(parseComposerDocument(value), {
        emitUpdate: false,
      });
    }
  }, [editor, value]);

  useEffect(() => {
    if (!editor) return;
    // 可编辑状态切换不是用户编辑，不能把尚未同步的文档回写到受控草稿。
    editor.setEditable(!disabled, false);
  }, [disabled, editor]);

  useEffect(() => {
    const {
      attachmentKeys,
      tokenKeys,
      unmatchedTokenKeys,
      tokenlessAttachmentKeys,
    } = reconciliation;
    const signature = `${unmatchedTokenKeys.join(",")}|${tokenlessAttachmentKeys.join(",")}|${suspendAttachmentReconciliation}|${disabled}`;
    if (signature === reconciliationSignatureRef.current) return;
    reconciliationSignatureRef.current = signature;

    if (unmatchedTokenKeys.length > 0) {
      const stage = suspendAttachmentReconciliation ? "同步中" : "已失效";
      void frontendLog(
        "WARNING",
        "inline_attachment_token_unmatched",
        `输入框存在没有附件对象的 token，显示为「附件${stage}」`,
        {
          data: {
            unmatchedTokenKeys,
            attachmentKeys,
            tokenKeys,
            suspendAttachmentReconciliation,
            disabled,
          },
        },
      );
      return;
    }
    if (tokenlessAttachmentKeys.length > 0) {
      void frontendLog(
        "DEBUG",
        "inline_attachment_token_pending",
        "输入框附件尚未写入对应 token",
        {
          data: {
            tokenlessAttachmentKeys,
            attachmentKeys,
            tokenKeys,
            suspendAttachmentReconciliation,
            disabled,
          },
        },
      );
    }
  }, [disabled, reconciliation, suspendAttachmentReconciliation]);

  const insertCommand = useCallback(
    (command: Parameters<typeof insertionCommandContent>[0]) => {
      if (!editor || !editor.isEditable) return;
      const content = insertionCommandContent(command);
      if (content.length === 0) return;
      const position = resolveComposerInsertionPosition(
        hasCapturedSelectionRef.current,
        editor.state.selection.anchor,
        editor.state.doc.content.size,
      );
      editor
        .chain()
        .focus()
        .setTextSelection(position)
        .insertContent(content)
        .run();
      hasCapturedSelectionRef.current = true;
    },
    [editor],
  );

  useEffect(
    () => insertion.register(insertCommand),
    [insertion, insertCommand],
  );

  useEffect(() => {
    if (
      !editor ||
      !referenceBridge ||
      !target ||
      referenceBridge.target !== target
    )
      return;
    return referenceBridge.register(
      target,
      (references, question, replacement) => {
        if (!editor.isEditable) {
          showFeedback("输入框暂不可编辑，请稍后添加引用");
          return;
        }
        const keys = new Set(
          decodeReferences(serializeComposerDocument(editor.getJSON())).flatMap(
            (part) =>
              part.type === "reference"
                ? [referenceIdentity(part.reference)]
                : [],
          ),
        );
        const unique = references.filter((ref) => {
          const key = referenceIdentity(ref);
          if (keys.has(key)) return false;
          keys.add(key);
          return true;
        });
        const content = [
          ...insertionCommandContent({
            type: "insert-references",
            references: unique,
          }),
          ...insertionCommandContent({
            type: "insert-text",
            text: question ? ` ${question}` : "",
          }),
        ];
        // @ 准备失败不修改草稿；等待期间用户改过该文本时也不删除新输入。
        const replace =
          replacement &&
          replacement.to <= editor.state.doc.content.size &&
          editor.state.doc.textBetween(replacement.from, replacement.to) ===
            replacement.text;
        const position = replace
          ? replacement.from
          : resolveComposerInsertionPosition(
              hasCapturedSelectionRef.current,
              editor.state.selection.anchor,
              editor.state.doc.content.size,
            );
        const chain = editor.chain().focus();
        if (replace) chain.deleteRange(replacement);
        chain.setTextSelection(position).insertContent(content).run();
        hasCapturedSelectionRef.current = true;
        if (!unique.length) showFeedback("这些引用已在当前草稿中");
      },
    );
  }, [
    editor,
    referenceBridge?.register,
    referenceBridge?.target,
    target,
    showFeedback,
  ]);

  /**
   * 禁用期间（运行中、草稿同步中等）的粘贴 / 拖放文件。
   *
   * ProseMirror 对不可编辑的编辑器仍会调用 handlePaste / handleDrop；文件输入已被 preventDefault，
   * 若不提示，用户看到的就是文件「凭空消失」。返回 true 表示已拒收。
   */
  const rejectFilesWhileDisabled = (
    source: "paste" | "drop",
    fileCount: number,
  ): boolean => {
    if (!propsRef.current.disabled) return false;
    notify.info("当前无法添加附件：输入框暂不可编辑，请稍后再试");
    void frontendLog(
      "INFO",
      "inline_attachment_rejected_while_disabled",
      "输入框禁用期间拒收粘贴或拖放的文件",
      {
        data: { source, fileCount },
      },
    );
    return true;
  };

  const handleExternalFiles = useCallback(
    async (files: readonly File[]) => {
      const current = propsRef.current;
      if (files.length === 0) return;
      if (!current.onExternalFiles) {
        void frontendLog(
          "INFO",
          "inline_attachment_external_files_ignored",
          "输入框未装配外部附件处理器，已忽略文件输入",
          {
            data: { fileCount: files.length },
          },
        );
        feedback.show("此输入框暂不支持添加附件。");
        return;
      }
      try {
        const result = await current.onExternalFiles(files);
        if (propsRef.current.disabled) return;
        if (result.attachments.length > 0) {
          insertion.dispatch({
            type: "insert-attachments",
            attachments: result.attachments,
          });
        }
        const rejectedFiles = result.problems.filter(
          (problem) => problem.reason === "unsupported-image",
        );
        if (rejectedFiles.length > 0) {
          void frontendLog(
            "WARNING",
            "composer_external_image_rejected",
            "已跳过不支持的图片附件",
            {
              data: {
                count: rejectedFiles.length,
                names: rejectedFiles.map((problem) => problem.name),
              },
            },
          );
        }
        const problemMessage = summarizeComposerFileProblems(result.problems);
        if (problemMessage) feedback.show(problemMessage);
        else feedback.clear();
      } catch (error) {
        feedback.show(userErrorMessage(error, "添加附件失败，请重试"));
        void frontendLog(
          "ERROR",
          "inline_attachment_external_files_failed",
          "处理粘贴或拖拽附件失败",
          {
            data: { fileCount: files.length },
            error,
          },
        );
      }
    },
    [feedback, insertion],
  );

  const captureUserSelection = (target: EventTarget | null) => {
    if (
      !editor ||
      !(target instanceof Node) ||
      !editor.view.dom.contains(target)
    )
      return;
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || !selection.anchorNode)
      return;
    if (!editor.view.dom.contains(selection.anchorNode)) return;
    hasCapturedSelectionRef.current = true;
  };

  useEffect(() => {
    // 从树导航打开任务时保留键盘焦点，避免异步挂载的输入框中断方向键与 Tab 操作。
    if (autoFocus && !document.activeElement?.closest('[role="tree"]')) editor?.commands.focus();
  }, [autoFocus, editor]);

  const attachmentView = useMemo(
    () => ({
      attachments,
      disabled,
      unmatchedAsPending: suspendAttachmentReconciliation,
      onRemoveAttachment,
    }),
    [
      attachments,
      disabled,
      onRemoveAttachment,
      suspendAttachmentReconciliation,
    ],
  );

  return (
    <ComposerEditorViewProvider value={attachmentView}>
      <ReferenceMention
        editor={editor}
        bridge={referenceBridge}
        target={target ?? null}
      />
      <EditorContent
        editor={editor}
        onFocus={(event) => {
          captureUserSelection(event.target);
        }}
        onMouseUp={(event) => captureUserSelection(event.target)}
        onKeyUp={(event) => captureUserSelection(event.target)}
        onCompositionStart={() => {
          isComposingRef.current = true;
        }}
        onCompositionEnd={() => {
          isComposingRef.current = false;
        }}
      />
    </ComposerEditorViewProvider>
  );
}
