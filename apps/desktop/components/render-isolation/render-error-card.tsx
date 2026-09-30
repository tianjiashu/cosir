import type { RenderIsolationScope } from "./render-error-types";

type RenderErrorCardProps = {
  scope: RenderIsolationScope;
  onRetry: () => void;
  label?: string;
  fallbackText?: string;
};

const SCOPE_LABELS: Record<RenderIsolationScope, string> = {
  message: "消息",
  part: "内容片段",
  markdown: "Markdown 内容",
  tool: "工具结果",
  attachment: "附件",
};

/**
 * 渲染失败后的最小安全 UI。
 *
 * 该组件只使用原生文本、pre 和 button，不依赖 Markdown、图标、工具
 * renderer 或其它容易失败的展示层，避免错误 fallback 再次触发渲染异常。
 * `fallbackText` 只作为纯文本展示，不会被当作 HTML 或 Markdown 解析。
 */
export function RenderErrorCard({
  scope,
  onRetry,
  label,
  fallbackText,
}: RenderErrorCardProps) {
  const displayLabel = label ?? SCOPE_LABELS[scope];

  return (
    <div
      role="status"
      className="my-1 flex min-w-0 flex-col gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm"
    >
      <div className="flex min-w-0 items-center justify-between gap-3">
        <span className="text-destructive">{displayLabel}暂时无法显示</span>
        <button
          type="button"
          className="shrink-0 text-xs underline underline-offset-4"
          onClick={onRetry}
        >
          重试显示
        </button>
      </div>
      {fallbackText ? (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs text-muted-foreground">
          {fallbackText}
        </pre>
      ) : null}
    </div>
  );
}
