"use client";

import { StreamdownTextPrimitive } from "@assistant-ui/react-streamdown";
import type { MessagePartStatus } from "@assistant-ui/core";

import { markdownPlugins } from "@/components/markdown-rendering-config";

type MarkdownTextProps = {
  status?: MessagePartStatus;
};

/**
 * Assistant 正文和 reasoning 的唯一 Markdown 边界。
 * Streamdown 负责不完整 Markdown、代码高亮、公式、Mermaid 图表、CJK 标点、
 * 流式 caret 和链接安全；组件本身不解析内容，也不把渲染结果写回 runtime 或服务端。
 */
export function MarkdownText({ status }: MarkdownTextProps) {
  return (
    <StreamdownTextPrimitive
      mode="streaming"
      defer
      plugins={markdownPlugins}
      shikiTheme={["github-light", "github-dark"]}
      caret={status?.type === "running" ? "block" : undefined}
      linkSafety={{ enabled: true }}
      mermaid={{ config: { securityLevel: "strict" } }}
      security={{ allowedProtocols: ["https", "mailto"] }}
      className="aui-md !space-y-3 leading-6"
    />
  );
}
