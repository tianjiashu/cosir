import { useMemo, type ComponentProps } from "react";
import type { ExtraProps } from "streamdown";

import { inlineCodeLink, resolveLink } from "@/lib/links/resolve-link";
import { cn } from "@/lib/utils";
import { useOpenLink, type LinkSource } from "./use-open-link";

/** 与 Streamdown 默认链接、行内代码相同的排版；颜色与下划线在 app/markdown.css 统一定义。 */
const LINK_CLASS = "wrap-anywhere font-medium";
const INLINE_CODE_CLASS = "rounded bg-muted px-1.5 py-0.5 font-mono text-sm";

/**
 * Markdown 渲染面的链接组件（`a` 与行内代码），所有 Streamdown 实例共用。
 *
 * 只负责识别：`a` 的地址与行内代码里的文件路径交给 `useOpenLink` 解析和打开；解析不出目标的链接
 * （其他协议、页内锚点、工作区外路径）渲染为纯文本，不给出点了没反应的入口。保留 `<a href>` 语义
 * 便于键盘访问，点击时拦下默认导航。
 *
 * `documentDir` 只在工作区文档里给出，相对链接按文档目录解析；`webOnly` 用于与工作区无关的文本
 * （Agent 提示词、Skill 说明），其中的相对链接不指向当前工作区，只认网页。
 */
export function useMarkdownLinkComponents(
  source: LinkSource,
  { documentDir, webOnly = false }: { documentDir?: string; webOnly?: boolean } = {},
) {
  const opener = useOpenLink(source);
  return useMemo(() => {
    function MarkdownLink({ href, children, className, node, ...props }: ComponentProps<"a"> & ExtraProps) {
      void node; // hast 节点只供渲染器内部使用，不能透传成 DOM 属性。
      const target = !href ? null
        : webOnly ? resolveLink(href, { workspaceRoot: null })
          : opener.resolve(href, documentDir);
      if (!target) return <span className={className}>{children}</span>;
      return (
        <a
          {...props}
          href={href}
          data-streamdown="link"
          title={target.kind === "workspace" ? `在文件面板打开 ${target.path || "."}` : target.kind === "web" ? target.url : undefined}
          className={cn(LINK_CLASS, className)}
          onClick={(event) => {
            event.preventDefault();
            opener.open(target, event.currentTarget);
          }}
        >
          {children}
        </a>
      );
    }
    function InlineCode({ className, children, node, ...props }: ComponentProps<"code"> & ExtraProps) {
      void node;
      const target = typeof children === "string" && !webOnly ? inlineCodeLink(children, opener.workspaceRoot) : null;
      const code = (
        <code className={cn(INLINE_CODE_CLASS, className)} data-streamdown="inline-code" {...props}>
          {children}
        </code>
      );
      if (!target) return code;
      return (
        <button
          type="button"
          data-slot="workspace-code-link"
          title={`在文件面板打开 ${target.path}`}
          onClick={() => opener.open(target)}
          className="cursor-pointer align-baseline focus-visible:outline-2 focus-visible:outline-ring"
        >
          {code}
        </button>
      );
    }
    return { a: MarkdownLink, inlineCode: InlineCode };
  }, [opener, documentDir, webOnly]);
}
