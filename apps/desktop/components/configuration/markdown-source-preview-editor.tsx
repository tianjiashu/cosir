"use client";

import { useEffect, useRef, useState } from "react";
import { basicSetup, EditorView } from "codemirror";
import { Compartment, EditorState } from "@codemirror/state";
import { markdown } from "@codemirror/lang-markdown";
import { Streamdown } from "streamdown";

import {
  editorTheme,
  useEditorSyntaxTheme,
} from "@/components/code-editor-theme";
import { Button } from "@/components/ui/button";
import { markdownPlugins } from "@/components/markdown-rendering-config";
import { handleMarkdownKeydown } from "@/components/configuration/markdown-editor-shortcuts";

export type MarkdownEditorMode = "source" | "split" | "preview";

type MarkdownSourcePreviewEditorProps = {
  value: string;
  onChange?: (value: string) => void;
  initialMode?: MarkdownEditorMode;
  readOnly?: boolean;
  placeholder?: string;
  minHeight?: string;
  ariaLabel?: string;
};

function CodeMirrorSourceEditor({
  value,
  onChange,
  readOnly = false,
  placeholder,
  minHeight,
  ariaLabel = "Markdown 源码编辑器",
}: Omit<MarkdownSourcePreviewEditorProps, "initialMode">) {
  const syntaxTheme = useEditorSyntaxTheme();
  const themeCompartment = useRef(new Compartment());
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const suppressChangeRef = useRef(false);
  const onChangeRef = useRef(onChange);
  const [isEmpty, setIsEmpty] = useState(value.length === 0);

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
        EditorState.readOnly.of(readOnly),
        EditorView.editable.of(!readOnly),
        EditorView.contentAttributes.of({ "aria-label": ariaLabel }),
        EditorView.domEventHandlers({
          keydown: (event, view) => handleMarkdownKeydown(view, event),
        }),
        EditorView.updateListener.of((update) => {
          if (!update.docChanged) return;
          const nextValue = update.state.doc.toString();
          setIsEmpty(nextValue.length === 0);
          if (!suppressChangeRef.current) onChangeRef.current?.(nextValue);
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
    setIsEmpty(value.length === 0);
  }, [value]);

  useEffect(() => {
    viewRef.current?.dispatch({
      effects: themeCompartment.current.reconfigure(syntaxTheme),
    });
  }, [syntaxTheme]);

  return (
    <div className="relative h-full" style={{ minHeight }}>
      <div ref={containerRef} className="h-full" aria-label={ariaLabel} />
      {isEmpty && placeholder && (
        <div className="text-muted-foreground pointer-events-none absolute left-4 top-4 text-sm">
          {placeholder}
        </div>
      )}
    </div>
  );
}

const modeItems: Array<{ id: MarkdownEditorMode; label: string }> = [
  { id: "source", label: "源码" },
  { id: "split", label: "分栏" },
  { id: "preview", label: "预览" },
];

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
  ariaLabel = "Markdown 源码编辑器",
}: MarkdownSourcePreviewEditorProps) {
  const [mode, setMode] = useState<MarkdownEditorMode>(initialMode);
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
      className="aui-md h-full overflow-auto p-4 leading-6"
      style={{ minHeight }}
    >
      {value ? (
        <Streamdown
          mode="static"
          plugins={markdownPlugins}
          shikiTheme={["github-light", "github-dark"]}
          linkSafety={{ enabled: true }}
          mermaid={{ config: { securityLevel: "strict" } }}
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
    <div className="border-border/70 bg-card/70 overflow-hidden rounded-2xl border shadow-sm">
      <div className="bg-muted/30 flex items-center justify-end gap-1 border-b px-3 py-2">
        <div
          className="bg-background flex rounded-lg border p-0.5"
          role="tablist"
          aria-label="Markdown 编辑视图"
        >
          {modeItems.map((item) => (
            <Button
              key={item.id}
              type="button"
              variant={mode === item.id ? "secondary" : "ghost"}
              size="sm"
              role="tab"
              aria-selected={mode === item.id}
              onClick={() => setMode(item.id)}
            >
              {item.label}
            </Button>
          ))}
        </div>
      </div>
      {mode === "source" && <div>{source}</div>}
      {mode === "preview" && <div>{preview}</div>}
      {mode === "split" && (
        <div className="grid divide-y lg:grid-cols-2 lg:divide-x lg:divide-y-0">
          {source}
          {preview}
        </div>
      )}
    </div>
  );
}
