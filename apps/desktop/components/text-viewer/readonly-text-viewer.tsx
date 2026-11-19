import { EditorView, lineNumbers } from "@codemirror/view";
import { useEffect, useImperativeHandle, useMemo, useRef, type Ref } from "react";
import { Compartment, EditorState, EditorSelection } from "@codemirror/state";
import { gotoLine } from "@codemirror/search";
import { frontendLog } from "@/lib/logging/frontend-log";
import { languageFor } from "@/lib/syntax/code-language";
import { editorTheme } from "@/components/code-editor/code-editor-theme";
import { editorSetup } from "@/components/code-editor/editor-setup";
import type { LineRange } from "@/lib/links/resolve-link";
import type { ReadingAnchor } from "@/lib/reading/reading-anchor";
import type { FindDirection, FindResult } from "@/lib/find/next-match";
import { findInEditor, refreshFindInEditor } from "@/components/code-editor/editor-find";

/** 父组件经 ref 驱动编辑器（查找条、大纲都在编辑器外）；编辑器尚未建好时 `find` 返回 null、`revealLine` 不做事。 */
export type TextViewerHandle = {
  find: (term: string, direction: FindDirection) => FindResult | null;
  /** 文本变了以后按同一个词重算命中，不移动选区。 */
  refresh: (term: string) => FindResult | null;
  /** 选中当前文本的第 `line` 行（1 起，超出末行收到末行）并滚到视口顶部附近。 */
  revealLine: (line: number) => void;
  /** 打开 CodeMirror 自带的跳转到行面板（输入行号或 `行:列`）。 */
  goToLine: () => void;
};

export type TextSelection = {
  from: number;
  to: number;
  text: string;
  startLine?: number;
  endLine?: number;
};

/** 编辑器只消费文本；不读取文件、不保存内容。异步语言加载在卸载后不再写 view。 */
export function ReadonlyTextViewer({
  text,
  name,
  wrap,
  anchor,
  onAnchorChange,
  onSelection,
  firstLine = 1,
  ariaLabel,
  selectLines = false,
  reveal,
  onRevealed,
  ref,
}: {
  text: string;
  name: string;
  wrap: boolean;
  /** 打开时恢复到的阅读位置；不给时从开头读。 */
  anchor?: ReadingAnchor;
  /** 用户滚动后的阅读位置（调用方要在重建后恢复时才给）。 */
  onAnchorChange?: (anchor: ReadingAnchor) => void;
  firstLine?: number;
  ariaLabel: string;
  selectLines?: boolean;
  onSelection?: (selection: TextSelection | null) => void;
  /** 要选中并居中的行（相对本段文本，1 起算）；超出末行时收到末行。处理后回调 `onRevealed`。 */
  reveal?: LineRange;
  onRevealed?: () => void;
  ref?: Ref<TextViewerHandle>;
}) {
  const highlight = useMemo(
    () => text.split("\n").every((line) => line.length < 50_000),
    [text],
  );
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  useImperativeHandle(ref, () => ({
    find: (term, direction) => (view.current ? findInEditor(view.current, term, direction) : null),
    refresh: (term) => (view.current ? refreshFindInEditor(view.current, term) : null),
    goToLine: () => {
      if (view.current) gotoLine(view.current);
    },
    revealLine: (line) => {
      const instance = view.current;
      if (!instance) return;
      const target = instance.state.doc.line(Math.min(Math.max(line, 1), instance.state.doc.lines));
      instance.dispatch({
        selection: EditorSelection.range(target.from, target.to),
        effects: EditorView.scrollIntoView(target.from, { y: "start", yMargin: 24 }),
      });
    },
  }), []);
  const latest = useRef({ onAnchorChange, anchor, onSelection });
  latest.current = { onAnchorChange, anchor, onSelection };
  const onRevealedRef = useRef(onRevealed);
  onRevealedRef.current = onRevealed;
  useEffect(() => {
    if (!host.current) return;
    let disposed = false;
    const language = new Compartment();
    const instance = new EditorView({
      parent: host.current,
      // 交给 CodeMirror 在测量之后再滚过去：建好就写像素会落在估算行高的布局上，测量后锚点修正把位置挪走。
      scrollTo: latest.current.anchor && scrollToAnchor(EditorState.create({ doc: text }), latest.current.anchor),
      state: EditorState.create({
        doc: text,
        extensions: [
          editorSetup,
          lineNumbers({
            formatNumber: (line) => String(line + firstLine - 1),
            domEventHandlers: selectLines
              ? {
                  mousedown: (editor, line, event) => {
                    const mouse = event as MouseEvent;
                    const anchor = mouse.shiftKey
                      ? editor.state.selection.main.anchor
                      : line.from;
                    editor.dispatch({
                      selection: EditorSelection.range(anchor, line.to),
                      scrollIntoView: true,
                    });
                    editor.focus();
                    return true;
                  },
                }
              : undefined,
          }),
          EditorView.updateListener.of((update) => {
            if (update.selectionSet || update.docChanged) {
              const range = update.state.selection.main;
              latest.current.onSelection?.(
                range.empty
                  ? null
                  : {
                      from: range.from,
                      to: range.to,
                      text: update.state.sliceDoc(range.from, range.to),
                      startLine: update.state.doc.lineAt(range.from).number,
                      endLine: update.state.doc.lineAt(
                        Math.max(range.from, range.to - 1),
                      ).number,
                    },
              );
            }
          }),
          editorTheme,
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          EditorView.contentAttributes.of({
            tabindex: "0",
            "aria-label": ariaLabel,
          }),
          language.of([]),
          ...(wrap ? [EditorView.lineWrapping] : []),
          EditorView.domEventHandlers({
            scroll: (_event, editor) => latest.current.onAnchorChange?.(anchorOf(editor)),
          }),
        ],
      }),
    });
    view.current = instance;
    // 超长单行仅关闭语法解析，保留阅读与查找，避免解析器占满主线程。
    if (highlight) {
      const description = languageFor(name);
      void description
        ?.load()
        .then((extension) => {
          if (!disposed)
            instance.dispatch({ effects: language.reconfigure(extension) });
        })
        .catch((error) => {
          void frontendLog(
            "WARNING",
            "text_language_load_failed",
            "语言高亮加载失败，保留纯文本阅读",
            { data: { name }, error },
          );
        });
    }
    return () => {
      disposed = true;
      instance.destroy();
      view.current = null;
    };
  }, [name, wrap, highlight, firstLine, ariaLabel, selectLines]);
  useEffect(() => {
    const instance = view.current;
    if (!instance || instance.state.doc.toString() === text) return;
    // 文件被改：整段替换后按行号回到原来那段，而不是同一个像素（内容变了，像素指向别处）。
    const kept = anchorOf(instance);
    const changes = { from: 0, to: instance.state.doc.length, insert: text };
    instance.dispatch({ changes, effects: scrollToAnchor(instance.state.update({ changes }).state, kept) });
  }, [text]);
  // 排在建 view 的 effect 之后：首次挂载时先恢复阅读位置，再由这里覆盖为目标行。
  useEffect(() => {
    const instance = view.current;
    if (!instance || !reveal) return;
    const doc = instance.state.doc;
    const first = doc.line(Math.min(reveal.start, doc.lines));
    const last = doc.line(Math.min(Math.max(reveal.end, reveal.start), doc.lines));
    instance.dispatch({
      selection: EditorSelection.range(first.from, last.to),
      effects: EditorView.scrollIntoView(first.from, { y: "center" }),
    });
    onRevealedRef.current?.();
  }, [reveal, text, wrap]);
  return <div ref={host} className="h-full min-h-0 overflow-hidden" />;
}

/**
 * 视口顶部所在的行与偏移（按已测量的布局算）。偏移相对这一行首字符的字形顶部，而不是行块顶部：恢复用的
 * `scrollIntoView` 按首字符的位置对齐，两边参照不一致时每重建一次就差出行高与字高之差（实测 2px）并累积。
 */
function anchorOf(view: EditorView): ReadingAnchor {
  const top = Math.max(0, view.scrollDOM.scrollTop - view.documentPadding.top);
  const block = view.lineBlockAtHeight(top);
  const glyph = view.coordsAtPos(block.from, 1);
  const lineTop = glyph ? glyph.top - view.documentTop : block.top;
  return { line: view.state.doc.lineAt(block.from).number, offset: top - lineTop };
}

/** 滚到阅读位置的效果；行号超出时收到末行。 */
function scrollToAnchor(state: EditorState, anchor: ReadingAnchor) {
  const line = state.doc.line(Math.min(Math.max(anchor.line, 1), state.doc.lines));
  return EditorView.scrollIntoView(line.from, { y: "start", yMargin: -anchor.offset });
}
