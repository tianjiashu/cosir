import { SearchIcon } from "lucide-react";
import { createContext, useContext, useMemo, type ComponentProps } from "react";
import type { ExtraProps } from "streamdown";

import { cn } from "@/lib/utils";
import { LinkTargetIcon } from "./link-target-icon";
import type { LinkOpener } from "./use-open-link";

const InsideMarkdownLink = createContext(false);

/** 字重继承所在文字；颜色与下划线在 styles/markdown.css 统一定义。 */
const LINK_CLASS = "wrap-anywhere";
const INLINE_CODE_CLASS = "rounded bg-[var(--inline-code-background,var(--muted))] px-1.5 py-0.5 font-mono text-sm";

/**
 * Markdown 渲染面的链接组件（`a` 与行内代码），所有 Streamdown 实例共用。
 *
 * 只负责识别：`a` 的地址与行内代码里的文件路径交给调用方传入的打开器解析、描述和打开；解析不出目标的
 * 链接（其他协议、工作区外路径）渲染为纯文本，不给出点了没反应的入口。保留 `<a href>` 语义便于键盘访问，
 * 点击时拦下默认导航。打开到哪里（文件面板、Skill 阅读区）、认不认工作区路径都由打开器决定，这里没有开关。
 *
 * `documentDir` 只在文档里给出（相对打开器的根），相对链接按文档目录解析。
 *
 * `icons` 只在对话里开启：取站点图标要联网，打开一篇文档不应该为其中所有链接发请求。行内代码是字面内容，
 * 不带图标。
 */
export function useMarkdownLinkComponents(
  opener: LinkOpener,
  { documentDir, icons = false }: { documentDir?: string; icons?: boolean } = {},
) {
  return useMemo(() => {
    function MarkdownLink({ href, children, className, node, ...props }: ComponentProps<"a"> & ExtraProps) {
      void node; // hast 节点只供渲染器内部使用，不能透传成 DOM 属性。
      const target = href ? opener.resolve(href, documentDir) : null;
      if (!target) return <span className={className}>{children}</span>;
      return (
        <a
          {...props}
          href={href}
          data-streamdown="link"
          title={opener.describe(target)}
          className={cn(LINK_CLASS, className)}
          onClick={(event) => {
            event.preventDefault();
            opener.open(target, event.currentTarget);
          }}
        >
          {icons && <LinkTargetIcon target={target} className="mr-[0.25em]" />}
          <InsideMarkdownLink.Provider value>{children}</InsideMarkdownLink.Provider>
        </a>
      );
    }
    function InlineCode({ className, children, node, ...props }: ComponentProps<"code"> & ExtraProps) {
      void node;
      const insideLink = useContext(InsideMarkdownLink);
      const target = typeof children === "string" && !insideLink ? opener.resolveCode(children, documentDir) : null;
      const code = (
        <code className={cn(INLINE_CODE_CLASS, className)} data-streamdown="inline-code" {...props}>
          {children}
        </code>
      );
      if (!target) return code;
      if (target.kind === "filename-search") return (
        <span className="group/file-mention inline-flex items-baseline" data-slot="workspace-file-mention">
          {code}
          <button
            type="button"
            title={opener.describe(target)}
            aria-label={`查找文件 ${target.query}`}
            onClick={() => opener.open(target)}
            className="ml-0.5 inline-flex self-center rounded text-muted-foreground opacity-0 group-hover/file-mention:opacity-100 focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-ring [@media(hover:none)]:opacity-100"
          >
            <SearchIcon className="size-3.5" aria-hidden="true" />
          </button>
        </span>
      );
      return (
        <button
          type="button"
          data-slot="workspace-code-link"
          title={opener.describe(target)}
          onClick={() => opener.open(target)}
          className="cursor-pointer align-baseline focus-visible:outline-2 focus-visible:outline-ring"
        >
          {code}
        </button>
      );
    }
    return { a: MarkdownLink, inlineCode: InlineCode };
  }, [opener, documentDir, icons]);
}
