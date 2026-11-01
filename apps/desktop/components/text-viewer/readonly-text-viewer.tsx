import { lineNumbers } from "@codemirror/view";
import { useEffect, useImperativeHandle, useMemo, useRef, type Ref } from "react";
import { basicSetup, EditorView } from "codemirror";
import { Compartment, EditorState, EditorSelection } from "@codemirror/state";
import { LanguageDescription } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { frontendLog } from "@/lib/logging/frontend-log";
import {
  editorTheme,
  useEditorSyntaxTheme,
} from "@/components/code-editor-theme";
import type { LineRange } from "@/lib/links/resolve-link";
import type { FindDirection, FindResult } from "@/lib/find/next-match";
import { findHighlights, findInEditor } from "./editor-find";

/** 父组件经 ref 驱动编辑器（查找条、大纲都在编辑器外）；编辑器尚未建好时 `find` 返回 null、`revealLine` 不做事。 */
export type TextViewerHandle = {
  find: (term: string, direction: FindDirection) => FindResult | null;
  /** 选中当前文本的第 `line` 行（1 起，超出末行收到末行）并滚到视口顶部附近。 */
  revealLine: (line: number) => void;
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
  scrollTop,
  onScroll,
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
  scrollTop: number;
  onScroll: (top: number) => void;
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
  const syntaxTheme = useEditorSyntaxTheme();
  const themeCompartment = useRef(new Compartment());
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  useImperativeHandle(ref, () => ({
    find: (term, direction) => (view.current ? findInEditor(view.current, term, direction) : null),
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
  const latest = useRef({ onScroll, scrollTop, onSelection });
  latest.current = { onScroll, scrollTop, onSelection };
  const onRevealedRef = useRef(onRevealed);
  onRevealedRef.current = onRevealed;
  useEffect(() => {
    if (!host.current) return;
    let disposed = false;
    const language = new Compartment();
    const instance = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: text,
        extensions: [
          basicSetup,
          findHighlights,
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
          themeCompartment.current.of(syntaxTheme),
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
          EditorView.contentAttributes.of({
            tabindex: "0",
            "aria-label": ariaLabel,
          }),
          language.of([]),
          ...(wrap ? [EditorView.lineWrapping] : []),
          EditorView.domEventHandlers({
            scroll: (_event, editor) =>
              latest.current.onScroll(editor.scrollDOM.scrollTop),
          }),
        ],
      }),
    });
    view.current = instance;
    instance.scrollDOM.scrollTop = latest.current.scrollTop;
    // 超长单行仅关闭语法解析，保留阅读与查找，避免解析器占满主线程。
    if (highlight) {
      const description = LanguageDescription.matchFilename(languages, name);
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
    const top = instance.scrollDOM.scrollTop;
    instance.dispatch({
      changes: { from: 0, to: instance.state.doc.length, insert: text },
    });
    instance.scrollDOM.scrollTop = top;
  }, [text]);
  // 排在建 view 的 effect 之后：首次挂载时先恢复 scrollTop，再由这里覆盖为目标行。
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
  useEffect(() => {
    view.current?.dispatch({
      effects: themeCompartment.current.reconfigure(syntaxTheme),
    });
  }, [syntaxTheme]);
  return <div ref={host} className="h-full min-h-0 overflow-hidden" />;
}
