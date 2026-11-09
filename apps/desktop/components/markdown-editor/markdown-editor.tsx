import { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type Ref } from "react";
import { basicSetup, EditorView } from "codemirror";
import { Compartment, EditorState } from "@codemirror/state";
import { placeholder as editorPlaceholder } from "@codemirror/view";
import { markdown } from "@codemirror/lang-markdown";

import { editorTheme, useEditorSyntaxTheme } from "@/components/code-editor/code-editor-theme";
import { useMarkdownLinkComponents } from "@/components/links/markdown-links";
import { handleMarkdownKeydown } from "@/components/markdown-editor/markdown-editor-keymap";
import { StaticMarkdown } from "@/components/markdown/static-markdown";
import { ViewToggle } from "@/components/ui/view-toggle";

type SourceEditorHandle = {
  /** 视口顶部所在的源码行（1 起）。 */
  topLine: () => number;
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
  const syntaxTheme = useEditorSyntaxTheme();
  const themeCompartment = useRef(new Compartment());
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const suppressChangeRef = useRef(false);
  const onChangeRef = useRef(onChange);

  useImperativeHandle(ref, () => ({
    topLine: () => {
      const view = viewRef.current!;
      return view.state.doc.lineAt(view.lineBlockAtHeight(view.scrollDOM.scrollTop).from).number;
    },
  }), []);

  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  useEffect(() => {
    const state = EditorState.create({
      doc: value,
      extensions: [
        basicSetup,
        markdown(),
        editorTheme,
        toggleClearance,
        themeCompartment.current.of(syntaxTheme),
        EditorView.lineWrapping,
        // 占位由 CodeMirror 画在首行内容里，天然避开行号栏。
        ...(placeholder ? [editorPlaceholder(placeholder)] : []),
        EditorView.contentAttributes.of({ "aria-label": ariaLabel }),
        EditorView.domEventHandlers({
          keydown: (event, view) => handleMarkdownKeydown(view, event),
        }),
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
    // 只在挂载时建一次：value 与主题由下面两个 effect 增量同步，重建会丢掉光标与撤销历史。
  }, []);

  useEffect(() => {
    const view = viewRef.current;
    if (!view || view.state.doc.toString() === value) return;
    suppressChangeRef.current = true;
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: value },
    });
    suppressChangeRef.current = false;
  }, [value]);

  useEffect(() => {
    viewRef.current?.dispatch({
      effects: themeCompartment.current.reconfigure(syntaxTheme),
    });
  }, [syntaxTheme]);

  return <div ref={containerRef} className="h-full" aria-label={ariaLabel} />;
}

/** 把预览滚到不晚于 `line` 的最后一个标题；它之前没有标题时回到顶部。 */
function scrollPreviewToLine(host: HTMLElement, line: number) {
  let target: HTMLElement | null = null;
  // `data-source-line` 也打在正文块上（选区求行范围用），这里只认标题。
  for (const heading of host.querySelectorAll<HTMLElement>("[data-source-line]")) {
    if (!/^H[1-6]$/.test(heading.tagName)) continue;
    if (Number(heading.dataset.sourceLine) > line) break;
    target = heading;
  }
  host.scrollTop = target
    ? target.getBoundingClientRect().top - host.getBoundingClientRect().top + host.scrollTop
    : 0;
}

/**
 * Markdown 编辑器：源码与预览二选一，右上角一键切换；填满父容器，高度由调用方决定。
 *
 * 源码编辑器始终挂载，看预览时只是隐藏（`visibility` 而非卸载或 `display: none`），所以切回来光标、
 * 撤销历史和滚动位置都在；预览只在显示时渲染。切到预览时按源码视口顶部的行，滚到它所在的标题。
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
  const links = useMarkdownLinkComponents("configuration", { webOnly: true });

  useLayoutEffect(() => {
    if (rendered) scrollPreviewToLine(preview.current!, previewLine.current);
  }, [rendered]);

  const toggle = () => {
    if (!rendered) previewLine.current = source.current!.topLine();
    setRendered(!rendered);
  };

  return (
    <div className="bg-background relative h-full min-h-0 overflow-hidden rounded-xl border">
      <div className={rendered ? "invisible absolute inset-0" : "absolute inset-0"}>
        <SourceEditor ref={source} value={value} onChange={onChange} placeholder={placeholder} ariaLabel={ariaLabel} />
      </div>
      {rendered && (
        <div ref={preview} className="absolute inset-0 overflow-auto px-6 py-5">
          <div className="aui-md mx-auto max-w-3xl leading-6">
            <StaticMarkdown components={links} className="!space-y-3">{value}</StaticMarkdown>
          </div>
        </div>
      )}
      <ViewToggle rendered={rendered} onToggle={toggle} className="bg-background/80 absolute top-2 right-2 z-10 backdrop-blur-sm" />
    </div>
  );
}
