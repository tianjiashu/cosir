import { SearchIcon } from "lucide-react";
import { createContext, useContext, useMemo, type ComponentProps, type ReactNode } from "react";
import type { ExtraProps } from "streamdown";

import { cn } from "@/lib/utils";
import { LinkTargetIcon } from "./link-target-icon";
import type { LinkOpener } from "./use-open-link";

const InsideMarkdownLink = createContext(false);

/** 字重继承所在文字；颜色与下划线在 styles/markdown.css 统一定义。 */
const LINK_CLASS = "wrap-anywhere";
const INLINE_CODE_CLASS = "rounded bg-[var(--inline-code-background,var(--muted))] px-1.5 py-0.5 font-mono text-sm";

type MarkdownLinkScopeValue = { opener: LinkOpener; documentDir?: string; icons: boolean };

const MarkdownLinkScopeContext = createContext<MarkdownLinkScopeValue | null>(null);

/**
 * Markdown 渲染面的链接作用域：这一片 Markdown 里的链接与行内代码交给哪个打开器解析、描述和打开。每个渲染面
 * （对话线程、文件预览、Skill 详情、配置编辑器的预览）在 `StaticMarkdown` / `MarkdownText` 外层声明一次；
 * 打开到哪里（文件面板、Skill 阅读区）、认不认工作区路径都由打开器决定，这里没有开关。
 *
 * 用 context 而不是把打开器闭包进组件再换组件实例：Streamdown 最外层的缓存比较不看 `components`，正文不变
 * 就不重渲染，打开器换了（工作区信息晚于第一帧到达、所属会话变了）链接也不会跟着变——先到的消息里链接
 * 一直是纯文本，点击也一直用旧的打开器。context 的变化穿透任何一层缓存，只重渲染读它的链接。
 *
 * `documentDir` 只在文档里给出（相对打开器的根），相对链接按文档目录解析。`icons` 只在对话里开启：取站点
 * 图标要联网，打开一篇文档不应该为其中所有链接发请求。
 */
export function MarkdownLinkScope({ opener, documentDir, icons = false, children }: {
  opener: LinkOpener;
  documentDir?: string;
  icons?: boolean;
  children: ReactNode;
}) {
  const value = useMemo(() => ({ opener, documentDir, icons }), [opener, documentDir, icons]);
  return <MarkdownLinkScopeContext.Provider value={value}>{children}</MarkdownLinkScopeContext.Provider>;
}

/** 读当前链接作用域；渲染面没声明时直接报错，而不是退回 Streamdown 的默认链接（绕过打开器）。 */
export function useMarkdownLinkScope(): MarkdownLinkScopeValue {
  const scope = useContext(MarkdownLinkScopeContext);
  if (!scope) throw new Error("Markdown 渲染面要放在 MarkdownLinkScope 里，链接才知道交给哪个打开器");
  return scope;
}

/**
 * Markdown 的链接组件（`a` 与行内代码），所有 Streamdown 实例共用、身份恒定。只负责识别：解析不出目标的
 * 链接（其他协议、工作区外路径）渲染为纯文本，不给出点了没反应的入口。保留 `<a href>` 语义便于键盘访问，
 * 点击时拦下默认导航。行内代码是字面内容，不带图标。
 */
function MarkdownLink({ href, children, className, node, ...props }: ComponentProps<"a"> & ExtraProps) {
  void node; // hast 节点只供渲染器内部使用，不能透传成 DOM 属性。
  const { opener, documentDir, icons } = useMarkdownLinkScope();
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
  const { opener, documentDir } = useMarkdownLinkScope();
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

export const MARKDOWN_LINK_COMPONENTS = { a: MarkdownLink, inlineCode: InlineCode } as const;

