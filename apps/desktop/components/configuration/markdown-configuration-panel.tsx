"use client";

import { useEffect } from "react";
import { Loader2Icon, SaveIcon } from "lucide-react";

import { MarkdownSourcePreviewEditor } from "@/components/configuration/markdown-source-preview-editor";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { useMarkdownConfigurationDocument, type MarkdownConfigurationAdapter } from "@/hooks/use-markdown-configuration-document";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { estimateMarkdownTokens, type MarkdownDocumentContract } from "@/lib/markdown-document";
import { useUnsavedChanges } from "@/lib/unsaved-changes";

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
 * 当前正文和预算，保存动作仍由业务 API adapter 完成。未保存的草稿登记到 settings 范围的
 * 未保存守卫，由配置页在切换标签 / 返回工作区前统一询问。
 */
export function MarkdownConfigurationPanel<TDocument extends MarkdownDocumentContract>({
  adapter,
  placeholder,
  saveLabel,
  description,
  initialMode = "split",
}: MarkdownConfigurationPanelProps<TDocument>) {
  const state = useMarkdownConfigurationDocument(adapter);
  useUnsavedChanges("settings", adapter.label, state.dirty);
  const { save } = state;
  useEffect(() => {
    const handleSaveShortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        save();
      }
    };
    window.addEventListener("keydown", handleSaveShortcut);
    return () => window.removeEventListener("keydown", handleSaveShortcut);
  }, [save]);
  if (!state.document) {
    return state.loadError
      ? <ErrorState className="my-10" message={userErrorMessage(state.loadError, "读取 Markdown 配置失败")} onRetry={state.reload} retryLabel="重新读取" />
      : <LoadingState label="正在读取 Markdown 配置…" />;
  }

  const document = state.document;
  const estimatedTokens = estimateMarkdownTokens(state.content);
  const overBudget = estimatedTokens > document.max_tokens;
  const emptyForbidden = adapter.allowEmpty === false && !state.content.trim();

  return (
    <div className="space-y-4">
      {state.saveError !== null && <Alert tone="destructive">{userErrorMessage(state.saveError, "保存 Markdown 配置失败")}</Alert>}
      {description && (
        <p className="text-destructive text-xs font-semibold">
          {description}
        </p>
      )}
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
          <Button onClick={save} disabled={state.saving || overBudget || emptyForbidden}>
            {state.saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}
            {saveLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
