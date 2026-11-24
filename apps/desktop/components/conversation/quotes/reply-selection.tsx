import { useThreadViewport } from "@assistant-ui/react";
import { useCallback } from "react";

import { SelectionToolbar } from "@/components/text-selection/selection-toolbar";
import { useDomSelectionSource, useTextSelection } from "@/components/text-selection/use-text-selection";
import type { QuoteCapsule } from "@/lib/assistant/capsules";
import { MARKDOWN_BLOCK_ATTRIBUTE, markdownBlockSource } from "@/lib/markdown-block-sources";
import { markdownExcerpt } from "@/lib/text-selection/markdown-excerpt";

/**
 * 对话线程的选区来源：只认同一条助手回复里的正文（流式 Markdown 的块，见 `markdown-block-sources`），
 * 换成不带位置的摘录；浮条与评论框是通用的 `SelectionToolbar`。工作台里的只读线程同样挂它：
 * 摘录插进的是当前会话的输入框。
 */
export function ReplySelection({ workspaceId }: { workspaceId: number }) {
  const host = useThreadViewport((state) => state.element.viewport);
  const selection = useTextSelection();
  const resolve = useCallback((range: Range): QuoteCapsule | null => {
    const reply = host && selectedReply(range, host);
    if (!reply) return null;
    const text = markdownExcerpt(range, {
      blocks: [...reply.querySelectorAll<HTMLElement>(`[${MARKDOWN_BLOCK_ATTRIBUTE}]`)],
      sourceOf: markdownBlockSource,
    });
    return text ? { kind: "quote", text } : null;
  }, [host]);
  useDomSelectionSource(host, selection, resolve);
  return <SelectionToolbar state={selection} workspaceId={workspaceId} source="conversation" />;
}

/**
 * 选区所在的那条助手回复（`data-role="assistant"`）；两端不在同一条回复里时为 null。
 * 摘录只取一条回复：跨回复的选区说不清是哪段原话，宁可不出摘录入口。
 */
function selectedReply(range: Range, host: HTMLElement): HTMLElement | null {
  const replyOf = (node: Node) => {
    const element = node instanceof Element ? node : node.parentElement;
    const reply = element?.closest<HTMLElement>('[data-role="assistant"]') ?? null;
    return reply && host.contains(reply) ? reply : null;
  };
  const reply = replyOf(range.startContainer);
  return reply && reply === replyOf(range.endContainer) ? reply : null;
}
