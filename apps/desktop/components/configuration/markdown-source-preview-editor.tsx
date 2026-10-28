"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { basicSetup, EditorView } from "codemirror";
import { Compartment, EditorState } from "@codemirror/state";
import { placeholder as editorPlaceholder } from "@codemirror/view";
import { markdown } from "@codemirror/lang-markdown";
import { Streamdown } from "streamdown";

import {
  editorTheme,
  useEditorSyntaxTheme,
} from "@/components/code-editor-theme";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { markdownPlugins, useMermaidOptions } from "@/components/markdown-rendering-config";
import { handleMarkdownKeydown } from "@/components/configuration/markdown-editor-shortcuts";

export type MarkdownEditorMode = "source" | "split" | "preview";

type MarkdownSourcePreviewEditorProps = {
  value: string;
  onChange?: (value: string) => void;
  initialMode?: MarkdownEditorMode;
  readOnly?: boolean;
  placeholder?: string;
  minHeight?: string;
  /** 固定高度，源码与预览在框内各自滚动；不传则按 minHeight 随内容增长。 */
  height?: string;
  ariaLabel?: string;
  /** 工具栏左侧的补充信息（如生效说明）；右侧固定是视图切换。 */
  toolbarStart?: ReactNode;
};

function CodeMirrorSourceEditor({
  value,
  onChange,
  readOnly = false,
  placeholder,
  minHeight,
  ariaLabel = "Markdown 源码编辑器",
}: Pick<MarkdownSourcePreviewEditorProps, "value" | "onChange" | "readOnly" | "placeholder" | "minHeight" | "ariaLabel">) {
  const syntaxTheme = useEditorSyntaxTheme();
  const themeCompartment = useRef(new Compartment());
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const suppressChangeRef = useRef(false);
  const onChangeRef = useRef(onChange);

  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  useEffect(() => {
    if (!containerRef.current) return;
    const state = EditorState.create({
      doc: value,
      extensions: [
        basicSetup,
        markdown(),
        editorTheme,
        themeCompartment.current.of(syntaxTheme),
        EditorView.lineWrapping,
        // 占位由 CodeMirror 画在首行内容里，天然避开行号栏。
        ...(placeholder ? [editorPlaceholder(placeholder)] : []),
        EditorState.readOnly.of(readOnly),
        EditorView.editable.of(!readOnly),
        EditorView.contentAttributes.of({ "aria-label": ariaLabel }),
        EditorView.domEventHandlers({
          keydown: (event, view) => handleMarkdownKeydown(view, event),
        }),
        EditorView.updateListener.of((update) => {
          if (!update.docChanged) return;
          if (!suppressChangeRef.current) onChangeRef.current?.(update.state.doc.toString());
        }),
      ],
    });
    const view = new EditorView({ state, parent: containerRef.current });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, [readOnly]);

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

  return (
    <div ref={containerRef} className="h-full min-h-0" style={{ minHeight }} aria-label={ariaLabel} />
  );
}

const modeOptions = [
  { value: "source", label: "源码" },
  { value: "split", label: "分栏" },
  { value: "preview", label: "预览" },
] as const satisfies ReadonlyArray<{ value: MarkdownEditorMode; label: string }>;

/**
 * 提供 Markdown 源码、分栏和预览三种视图的受控编辑器。
 *
 * 源码由 CodeMirror 直接维护，预览由 Streamdown 从同一份 Markdown 字符串渲染；组件不负责
 * 保存、请求或业务校验，因此可嵌入独立配置页面和 Agent 表单。
 */
export function MarkdownSourcePreviewEditor({
  value,
  onChange,
  initialMode = "split",
  readOnly = false,
  placeholder,
  minHeight = "28rem",
  height,
  ariaLabel = "Markdown 源码编辑器",
  toolbarStart,
}: MarkdownSourcePreviewEditorProps) {
  const [mode, setMode] = useState<MarkdownEditorMode>(initialMode);
  const mermaidOptions = useMermaidOptions();
  const source = (
    <CodeMirrorSourceEditor
      value={value}
      onChange={onChange}
      readOnly={readOnly}
      placeholder={placeholder}
      minHeight={minHeight}
      ariaLabel={ariaLabel}
    />
  );
  const preview = (
    <div
      className="aui-md h-full min-h-0 overflow-auto p-4 leading-6"
      style={{ minHeight }}
    >
      {value ? (
        <Streamdown
          mode="static"
          plugins={markdownPlugins}
          shikiTheme={["github-light", "github-dark"]}
          linkSafety={{ enabled: true }}
          mermaid={mermaidOptions}
          className="!space-y-3"
        >
          {value}
        </Streamdown>
      ) : (
        <p className="text-muted-foreground text-sm">暂无预览内容</p>
      )}
    </div>
  );

  return (
    <div className="bg-background flex flex-col overflow-hidden rounded-xl border">
      <div className="bg-muted/30 flex min-h-11 items-center justify-between gap-3 border-b px-3 py-1.5">
        <div className="text-muted-foreground min-w-0 text-xs">{toolbarStart}</div>
        <SegmentedControl
          size="sm"
          value={mode}
          options={modeOptions}
          onValueChange={setMode}
          aria-label="Markdown 编辑视图"
          className="shrink-0"
        />
      </div>
      <div className="min-h-0" style={{ height }}>
        {mode === "source" && source}
        {mode === "preview" && preview}
        {mode === "split" && (
          <div className="grid h-full grid-rows-2 divide-y lg:grid-cols-2 lg:grid-rows-1 lg:divide-x lg:divide-y-0">
            {source}
            {preview}
          </div>
        )}
      </div>
    </div>
  );
}
