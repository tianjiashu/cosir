import { StreamdownTextPrimitive } from "@assistant-ui/react-streamdown";
import type { MessagePartStatus } from "@assistant-ui/core";

import { useMarkdownLinkComponents } from "@/components/links/markdown-links";
import { markdownPlugins, markdownRehypePlugins, useMermaidOptions } from "@/components/markdown-rendering-config";

type MarkdownTextProps = {
  status?: MessagePartStatus;
};

/**
 * Assistant 正文和 reasoning 的唯一 Markdown 边界。
 * Streamdown 负责不完整 Markdown、代码高亮、公式、Mermaid 图表、CJK 标点与流式 caret；
 * 组件本身不解析内容，也不把渲染结果写回 runtime 或服务端。链接与行内代码里的工作区路径
 * 经 `useMarkdownLinkComponents` 识别、由链接打开入口统一打开（所属 Task 来自 `LinkTaskContext`）。
 */
export function MarkdownText({ status }: MarkdownTextProps) {
  const mermaidOptions = useMermaidOptions();
  const components = useMarkdownLinkComponents("message");
  return (
    <StreamdownTextPrimitive
      mode="streaming"
      defer
      plugins={markdownPlugins}
      rehypePlugins={markdownRehypePlugins}
      shikiTheme={["github-light", "github-dark"]}
      caret={status?.type === "running" ? "block" : undefined}
      mermaid={mermaidOptions}
      components={components}
      className="aui-md !space-y-3 leading-6"
    />
  );
}
