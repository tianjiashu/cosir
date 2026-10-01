"use client";

import { useEffect } from "react";
import { CheckIcon, Loader2Icon, SaveIcon } from "lucide-react";

import { MarkdownSourcePreviewEditor } from "@/components/configuration/markdown-source-preview-editor";
import { Button } from "@/components/ui/button";
import { useMarkdownConfigurationDocument, type MarkdownConfigurationAdapter } from "@/hooks/use-markdown-configuration-document";
import { estimateMarkdownTokens, type MarkdownDocumentContract } from "@/lib/markdown-document";

function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null;
  return <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{message}</div>;
}

type MarkdownConfigurationPanelProps<TDocument extends MarkdownDocumentContract> = {
  adapter: MarkdownConfigurationAdapter<TDocument>;
  placeholder: string;
  saveLabel: string;
  description?: string;
  initialMode?: "source" | "split" | "preview";
};

/**
 * 组合 Markdown 编辑器与配置文档保存流程。
 *
 * 该组件只依赖文档 adapter，不区分系统级、workspace 级或 Agent 级来源；后端响应负责提供
 * 当前正文和预算，保存动作仍由业务 API adapter 完成。
 */
export function MarkdownConfigurationPanel<TDocument extends MarkdownDocumentContract>({
  adapter,
  placeholder,
  saveLabel,
  description,
  initialMode = "split",
}: MarkdownConfigurationPanelProps<TDocument>) {
  const state = useMarkdownConfigurationDocument(adapter);
  useEffect(() => {
    const handleSaveShortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void state.save();
      }
    };
    window.addEventListener("keydown", handleSaveShortcut);
    return () => window.removeEventListener("keydown", handleSaveShortcut);
  }, [state.save]);
  if (state.loading && !state.document) {
    return <div className="text-muted-foreground flex items-center gap-2 py-12 text-sm"><Loader2Icon className="size-4 animate-spin" />正在读取 Markdown 配置…</div>;
  }
  if (!state.document) {
    return <div className="space-y-3 py-12"><ErrorNotice message={state.error ?? "读取 Markdown 配置失败"} /><Button variant="outline" onClick={() => void state.load()}>重新读取</Button></div>;
  }

  const document = state.document;
  const estimatedTokens = estimateMarkdownTokens(state.content);
  const overBudget = estimatedTokens > document.max_tokens;
  const emptyForbidden = adapter.allowEmpty === false && !state.content.trim();

  return (
    <div className="space-y-4">
      <ErrorNotice message={state.error} />
      {description && <p className="text-muted-foreground text-xs">{description}</p>}
      <MarkdownSourcePreviewEditor
        value={state.content}
        onChange={state.setContent}
        initialMode={initialMode}
        placeholder={placeholder}
      />
      <div className="flex flex-wrap items-end justify-between gap-3 border-t pt-4">
        <div>
          <p className="text-muted-foreground text-xs">估算 Token</p>
          <p className={`mt-1 font-mono text-sm ${overBudget ? "text-destructive" : ""}`}>
            {estimatedTokens.toLocaleString()} / {document.max_tokens.toLocaleString()}
          </p>
          {state.dirty && <p className="text-muted-foreground mt-1 text-xs">有未保存更改</p>}
        </div>
        <div className="flex items-center gap-2">
          {state.saved && <span className="text-muted-foreground text-xs">已保存</span>}
          <Button onClick={() => void state.save()} disabled={state.saving || overBudget || emptyForbidden}>
            {state.saving ? <Loader2Icon className="animate-spin" /> : state.saved ? <CheckIcon /> : <SaveIcon />}
            {saveLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
