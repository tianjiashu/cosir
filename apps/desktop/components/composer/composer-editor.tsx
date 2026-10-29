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
  serializeComposerDocument,
  shouldPreservePendingEditorValue,
} from "@/components/composer/composer-document";
import { useComposerFeedback } from "@/components/composer/composer-feedback";
import { useComposerInsertion } from "@/components/composer/composer-insertion-context";
import { cn } from "@/lib/utils";

/** 新对话页与任务内输入框共用的占位文案：同一种语气（描述要做的事 + 一个具体例子）。 */
export const COMPOSER_PLACEHOLDER = "描述要做的事，例如：帮我查找登录相关代码…";

/** 一行起步、随内容长高到 `max-h-48` 后内部滚动；需要更大初始输入区的页面（新对话）自行叠加 `min-h-*`。 */
export const COMPOSER_EDITOR_CLASS_NAME =
  "text-foreground min-h-10 w-full bg-transparent px-2.5 py-2 text-base leading-6 outline-none focus-visible:outline-none data-[disabled=true]:cursor-not-allowed data-[disabled=true]:opacity-60";

type ComposerEditorProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onExternalFiles: (files: readonly File[]) => void | Promise<void>;
  placeholder?: string;
  autoFocus?: boolean;
  disabled?: boolean;
  hydrating?: boolean;
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
  onExternalFiles,
  placeholder,
  autoFocus = false,
  disabled = false,
  hydrating = false,
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
    active: !referenceBridge || referenceBridge.target === target,
    disabled,
    placeholder,
  });
  propsRef.current = {
    onChange,
    onSubmit,
    onExternalFiles,
    active: !referenceBridge || referenceBridge.target === target,
    disabled,
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
          "cosir-composer-editor min-w-0 max-h-48 w-full overflow-y-auto whitespace-pre-wrap break-words",
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
      handleDOMEvents: {
        paste: (_view, event) => acceptTransfer(event, event.clipboardData),
        drop: (_view, event) => acceptTransfer(event, event.dataTransfer),
        dragover: (_view, event) => {
          if (!event.dataTransfer?.types.includes("Files")) return false;
          event.preventDefault();
          return true;
        },
      },
      handlePaste: (_view, event) => {
        const text = event.clipboardData?.getData("text/plain") ?? "";
        if (!decodeReferences(text).some((part) => part.type === "reference")) return false;
        event.preventDefault();
        editor?.commands.insertContent(parseComposerDocument(text).content ?? []);
        return true;
      },
    },
  });

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
    () => insertion.register(insertCommand, () => !!editor && !editor.isDestroyed && !propsRef.current.disabled && propsRef.current.active),
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

  /** 文件事件只在这里读取；图片/普通文件策略及异步生命周期交给统一附件接入边界。 */
  const acceptTransfer = (event: ClipboardEvent | DragEvent, data: DataTransfer | null): boolean => {
    const files = [...(data?.files ?? [])];
    if (!files.length) return false;
    event.preventDefault();
    if (propsRef.current.disabled) {
      feedback.show("输入框暂不可编辑，请稍后添加附件");
      return true;
    }
    const text = data?.getData("text/plain") ?? "";
    if (text) {
      editor?.chain().focus().insertContent(insertionCommandContent({ type: "insert-text", text })).run();
      hasCapturedSelectionRef.current = true;
    }
    void propsRef.current.onExternalFiles(files);
    return true;
  };

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
    // 从树导航打开任务时保留键盘焦点，避免异步挂载的输入框中断方向键与 Tab 操作；
    // 任务行的「…」菜单渲染在树外的 portal 里，也要排除，否则菜单会被抢焦点关闭。
    if (autoFocus && !document.activeElement?.closest('[role="tree"], [role="menu"]')) editor?.commands.focus();
  }, [autoFocus, editor]);

  const attachmentView = useMemo(() => ({ disabled, hydrating }), [disabled, hydrating]);

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
