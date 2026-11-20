import { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type Ref } from "react";
import { EditorState, Prec } from "@codemirror/state";
import { EditorView, placeholder as editorPlaceholder } from "@codemirror/view";
import { markdown } from "@codemirror/lang-markdown";

import { editorTheme } from "@/components/code-editor/code-editor-theme";
import { editorSetup } from "@/components/code-editor/editor-setup";
import { editorHighlighting } from "@/components/code-editor/editor-highlighting";
import { minimalChange } from "@/components/code-editor/minimal-change";
import { markdownHighlighter } from "@/lib/syntax/highlighter";
import { findInEditor, refreshFindInEditor, replaceInEditor } from "@/components/code-editor/editor-find";
import { EditorFindScope } from "@/components/code-editor/editor-find-scope";
import type { FindDirection, FindResult } from "@/lib/find/next-match";
import { MarkdownLinkScope } from "@/components/links/markdown-links";
import { useWebOnlyLinkOpener } from "@/components/links/use-open-link";
import { handleMarkdownKeydown } from "@/components/markdown-editor/markdown-editor-keymap";
import { StaticMarkdown } from "@/components/markdown/static-markdown";
import { ViewToggle } from "@/components/ui/view-toggle";
import { scrollRenderedTo } from "@/lib/reading/rendered-anchor";

type SourceEditorHandle = {
  /** 视口顶部所在的源码行（1 起）。 */
  topLine: () => number;
  /** 应用查找条用：查找（空词清除高亮）、编辑后重算、替换。 */
  find: (term: string, direction: FindDirection) => FindResult;
  refresh: (term: string) => FindResult;
  replace: (term: string, replacement: string, all: boolean) => FindResult;
};

// 右上角浮着视图切换按钮，正文右侧让出它的宽度。
const toggleClearance = EditorView.theme({ ".cm-content": { paddingRight: "2.5rem" } });

function SourceEditor({ value, onChange, placeholder, ariaLabel, ref }: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  ariaLabel: string;
  ref: Ref<SourceEditorHandle>;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const suppressChangeRef = useRef(false);
  const onChangeRef = useRef(onChange);

  useImperativeHandle(ref, () => ({
    topLine: () => {
      const view = viewRef.current!;
      return view.state.doc.lineAt(view.lineBlockAtHeight(view.scrollDOM.scrollTop).from).number;
    },
    find: (term, direction) => findInEditor(viewRef.current!, term, direction),
    refresh: (term) => refreshFindInEditor(viewRef.current!, term),
    replace: (term, replacement, all) => replaceInEditor(viewRef.current!, term, replacement, all),
  }), []);

  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  useEffect(() => {
    const highlighter = markdownHighlighter();
    const state = EditorState.create({
      doc: value,
      extensions: [
        editorSetup,
        markdown(),
        ...(highlighter ? [editorHighlighting(highlighter)] : []),
        editorTheme,
        toggleClearance,
        EditorView.lineWrapping,
        // 占位由 CodeMirror 画在首行内容里，天然避开行号栏。
        ...(placeholder ? [editorPlaceholder(placeholder)] : []),
        EditorView.contentAttributes.of({ "aria-label": ariaLabel }),
        // 高于编辑器键表：Markdown 命令是内层，同键时它优先（登记表的 `overrides`，如斜体覆盖扩大选区）。
        Prec.highest(EditorView.domEventHandlers({
          keydown: (event, view) => handleMarkdownKeydown(view, event),
        })),
        EditorView.updateListener.of((update) => {
          if (!update.docChanged) return;
          if (!suppressChangeRef.current) onChangeRef.current(update.state.doc.toString());
        }),
      ],
    });
    const view = new EditorView({ state, parent: containerRef.current! });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // 只在挂载时建一次：value 由下面的 effect 增量同步，重建会丢掉光标与撤销历史；深浅色由 CSS 变量切换。
  }, []);

  useEffect(() => {
    const view = viewRef.current;
    const changes = view ? minimalChange(view.state, value) : null;
    if (!view || !changes) return;
    suppressChangeRef.current = true;
    view.dispatch({ changes });
    suppressChangeRef.current = false;
  }, [value]);

  return <div ref={containerRef} className="h-full" aria-label={ariaLabel} />;
}

/**
 * Markdown 编辑器：源码与预览二选一，右上角一键切换；填满父容器，高度由调用方决定。
 *
 * 源码编辑器始终挂载，看预览时只是隐藏（`visibility` 而非卸载或 `display: none`），所以切回来光标、
 * 撤销历史和滚动位置都在；预览只在显示时渲染。切到预览时按源码视口顶部的行，滚到它所在的那一块
 * （与文件面板同一条规则，`lib/reading/rendered-anchor.ts`）。
 * 组件不负责保存、请求或业务校验。
 */
export function MarkdownEditor({
  value,
  onChange,
  placeholder,
  ariaLabel = "Markdown 源码编辑器",
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  ariaLabel?: string;
}) {
  const [rendered, setRendered] = useState(false);
  const source = useRef<SourceEditorHandle>(null);
  const preview = useRef<HTMLDivElement>(null);
  const previewLine = useRef(1);
  const links = useWebOnlyLinkOpener("configuration");

  useLayoutEffect(() => {
    if (rendered) scrollRenderedTo(preview.current!, { line: previewLine.current, offset: 0 });
  }, [rendered]);

  const toggle = () => {
    if (!rendered) previewLine.current = source.current!.topLine();
    setRendered(!rendered);
  };

  return (
    <div className="bg-background relative h-full min-h-0 overflow-hidden rounded-xl border">
      <EditorFindScope
        target={{
          find: (term, direction) => source.current?.find(term, direction) ?? null,
          refresh: (term) => source.current?.refresh(term) ?? null,
          replace: (term, replacement, all) => source.current?.replace(term, replacement, all) ?? null,
        }}
        contentKey={value}
        label="在源码中查找"
        barClassName="top-2 right-12"
        className={rendered ? "invisible absolute inset-0" : "absolute inset-0"}
      >
        <SourceEditor ref={source} value={value} onChange={onChange} placeholder={placeholder} ariaLabel={ariaLabel} />
      </EditorFindScope>
      {rendered && (
        <div ref={preview} className="absolute inset-0 overflow-auto px-6 py-5">
          <div className="aui-md mx-auto max-w-3xl leading-6">
            <MarkdownLinkScope opener={links}>
              <StaticMarkdown className="!space-y-3">{value}</StaticMarkdown>
            </MarkdownLinkScope>
          </div>
        </div>
      )}
      <ViewToggle rendered={rendered} onToggle={toggle} className="bg-background/80 absolute top-2 right-2 z-10 backdrop-blur-sm" />
    </div>
  );
}
