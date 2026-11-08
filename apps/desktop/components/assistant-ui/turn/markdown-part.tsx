import { useAuiState } from "@assistant-ui/react";

import { MarkdownText } from "@/components/markdown/markdown-text";
import { PartRenderBoundary } from "@/components/assistant-ui/elements/part-render-boundary";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";

/**
 * 交给 `MessagePrimitive.PartByIndex` 的正文渲染器：在 part 作用域内套渲染隔离边界（边界要读
 * `state.part` 的身份与正文），Markdown 出错只影响这一段、重试只重挂这一段。
 */
function MarkdownPart({ renderer, label }: { renderer: string; label: string }) {
  const text = useAuiState((state) => (state.part.type === "text" ? state.part.text : ""));
  return (
    <PartRenderBoundary
      renderer={renderer}
      fallback={({ onRetry }) => <RenderErrorCard scope="markdown" label={label} fallbackText={text} onRetry={onRetry} />}
    >
      <MarkdownText />
    </PartRenderBoundary>
  );
}

/** 正文（叙述与结论）的 `PartByIndex` 组件表。 */
export const TEXT_PART_COMPONENTS = {
  Text: () => <MarkdownPart renderer="Assistant Markdown" label="Assistant 内容" />,
};
