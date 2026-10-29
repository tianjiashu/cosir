"use client";

import { StreamdownTextPrimitive } from "@assistant-ui/react-streamdown";
import type { MessagePartStatus } from "@assistant-ui/core";
import type { ComponentProps } from "react";
import type { ExtraProps } from "streamdown";

import { markdownPlugins, useMermaidOptions } from "@/components/markdown-rendering-config";
import { useOpenWorkspacePath } from "@/components/workspace-references/use-open-workspace-path";
import { pathFromInlineCode } from "@/lib/workspace-files/workspace-path";
import { cn } from "@/lib/utils";

/** 与 Streamdown 默认行内代码相同的外观；覆盖组件后需自行保留。 */
const INLINE_CODE_CLASS = "rounded bg-muted px-1.5 py-0.5 font-mono text-sm";

/**
 * 行内代码里的工作区文件路径（如 `src/app.ts:42`）可点击在文件面板打开。
 *
 * 只认当前工作区内的路径，其余行内代码保持原样；不访问文件、不改动正文。
 */
function InlineCode({ className, children, node, ...props }: ComponentProps<"code"> & ExtraProps) {
  void node; // hast 节点只供渲染器内部使用，不能透传成 DOM 属性。
  const opener = useOpenWorkspacePath();
  const text = typeof children === "string" ? children : null;
  const raw = text === null ? null : pathFromInlineCode(text);
  const target = raw === null ? null : opener?.resolve(raw) ?? null;
  const code = (
    <code className={cn(INLINE_CODE_CLASS, className)} data-streamdown="inline-code" {...props}>
      {children}
    </code>
  );
  if (!opener || target === null) return code;
  return (
    <button
      type="button"
      title={`在文件面板打开 ${target}`}
      onClick={() => opener.openFile(target)}
      className="cursor-pointer align-baseline underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring"
    >
      {code}
    </button>
  );
}

const MARKDOWN_COMPONENTS = { inlineCode: InlineCode };

type MarkdownTextProps = {
  status?: MessagePartStatus;
};

/**
 * Assistant 正文和 reasoning 的唯一 Markdown 边界。
 * Streamdown 负责不完整 Markdown、代码高亮、公式、Mermaid 图表、CJK 标点、
 * 流式 caret 和链接安全；组件本身不解析内容，也不把渲染结果写回 runtime 或服务端。
 * 唯一的自定义是行内代码里的工作区路径可点击打开。
 */
export function MarkdownText({ status }: MarkdownTextProps) {
  const mermaidOptions = useMermaidOptions();
  return (
    <StreamdownTextPrimitive
      mode="streaming"
      defer
      plugins={markdownPlugins}
      shikiTheme={["github-light", "github-dark"]}
      caret={status?.type === "running" ? "block" : undefined}
      linkSafety={{ enabled: true }}
      mermaid={mermaidOptions}
      security={{ allowedProtocols: ["https", "mailto"] }}
      components={MARKDOWN_COMPONENTS}
      className="aui-md !space-y-3 leading-6"
    />
  );
}
