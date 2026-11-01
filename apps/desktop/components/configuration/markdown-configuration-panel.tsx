import { useHotkeys } from "react-hotkeys-hook";
import { Loader2Icon, SaveIcon } from "lucide-react";

import { MarkdownEditor } from "@/components/markdown-editor/markdown-editor";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { useConfigurationDocument, type ConfigurationDocumentAdapter } from "@/components/configuration/use-configuration-document";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { estimateMarkdownTokens, type MarkdownDocumentContract } from "@/lib/markdown-document";
import { useUnsavedChanges } from "@/lib/unsaved-changes";
import { APP_SHORTCUTS, shortcutLabel } from "@/lib/keyboard/shortcuts";

type MarkdownConfigurationPanelProps = {
  adapter: ConfigurationDocumentAdapter<MarkdownDocumentContract>;
  placeholder: string;
  saveLabel: string;
  description?: string;
  /** 正文不能为空（如主 Agent 提示词）；缺省允许保存空文档。 */
  requireContent?: boolean;
};

/**
 * 组合 Markdown 编辑器与配置文档保存流程。
 *
 * 该组件只依赖文档 adapter，不区分系统级、workspace 级或 Agent 级来源；后端响应负责提供
 * 当前正文和预算，保存动作仍由业务 API adapter 完成。未保存的草稿登记到 settings 范围的
 * 未保存守卫，由配置页在切换标签 / 返回工作区前统一询问。
 */
export function MarkdownConfigurationPanel({
  adapter,
  placeholder,
  saveLabel,
  description,
  requireContent = false,
}: MarkdownConfigurationPanelProps) {
  const state = useConfigurationDocument(adapter);
  const document = state.document;
  const estimatedTokens = estimateMarkdownTokens(state.content);
  const overBudget = document !== undefined && estimatedTokens > document.max_tokens;
  // 按钮与快捷键共用这一判定：超预算或必填却为空时都不发请求（后端同样会拒绝）。
  const blocked = overBudget || (requireContent && !state.content.trim());
  const save = () => {
    if (!blocked) state.save();
  };
  useUnsavedChanges("settings", adapter.label, state.dirty);
  useHotkeys(
    APP_SHORTCUTS.saveConfiguration.keys,
    save,
    { enableOnFormTags: true, enableOnContentEditable: true, preventDefault: true },
    [blocked, state.save],
  );
  if (!document) {
    return state.loadError
      ? <ErrorState className="my-10" message={userErrorMessage(state.loadError, "读取 Markdown 配置失败")} onRetry={state.reload} retryLabel="重新读取" />
      : <LoadingState label="正在读取 Markdown 配置…" />;
  }

  return (
    // 填满配置页内容区的剩余高度：长文在编辑器里滚动，保存栏始终可见；窗口太矮时编辑器保底高度、由内容区滚动。
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      {state.saveError !== null && <Alert tone="destructive">{userErrorMessage(state.saveError, "保存 Markdown 配置失败")}</Alert>}
      <div className="min-h-64 flex-1">
        <MarkdownEditor value={state.content} onChange={state.setContent} placeholder={placeholder} />
      </div>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="text-muted-foreground space-y-1 text-xs">
          {description && <p>{description}</p>}
          <p>
            <span>估算 Token</span>{" "}
            <span className={`font-mono ${overBudget ? "text-destructive" : "text-foreground"}`}>
              {estimatedTokens.toLocaleString()} / {document.max_tokens.toLocaleString()}
            </span>
            {state.dirty && <>{" · "}<span>有未保存更改</span></>}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={save} disabled={state.saving || blocked}>
            {state.saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}
            {saveLabel}
            <Kbd aria-hidden="true" className="text-primary-foreground/70 h-auto bg-transparent px-0">{shortcutLabel("saveConfiguration")}</Kbd>
          </Button>
        </div>
      </div>
    </div>
  );
}
