"use client";

import { StreamdownTextPrimitive } from "@assistant-ui/react-streamdown";
import type { MessagePartStatus } from "@assistant-ui/core";

type MarkdownTextProps = {
  status?: MessagePartStatus;
};

/**
 * Assistant 正文和 reasoning 的唯一 Markdown 边界。
 * Streamdown 负责不完整 Markdown、代码块、流式 caret 和链接安全；组件本身不
 * 解析内容，也不把渲染结果写回 runtime 或服务端。
 */
export function MarkdownText({ status }: MarkdownTextProps) {
  return (
    <StreamdownTextPrimitive
      mode="streaming"
      defer
      caret={status?.type === "running" ? "block" : undefined}
      linkSafety={{ enabled: true }}
      security={{ allowedProtocols: ["https", "mailto"] }}
      className="aui-md"
    />
  );
}
