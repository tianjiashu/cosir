import { useMemo } from "react";
import { Streamdown, type StreamdownProps } from "streamdown";

import { MARKDOWN_LINK_COMPONENTS } from "@/components/links/markdown-links";
import { cn } from "@/lib/utils";

import { markdownPlugins, markdownRehypePlugins, useMermaidOptions } from "@/components/markdown/markdown-rendering-config";
import { rehypeSourceLines } from "@/lib/markdown-outline";

// 模块级单例：插件数组每次渲染都新建会让 Streamdown 重新解析整篇文档。
const staticRehypePlugins = [...markdownRehypePlugins, rehypeSourceLines];

/**
 * 整篇一次渲染的 Markdown（文件预览、配置预览、Skill 说明、带胶囊的用户消息）；流式的助手消息走 `MarkdownText`。
 *
 * 解析插件、HTML 清洗、代码高亮主题与 Mermaid 配色只在这里装配。块级元素带 `data-source-line`
 * （源码行号），阅读器的大纲跳转、阅读位置、编辑器切到预览时的定位与选区求行范围都按它找。
 *
 * 根节点就是 `.aui-md`：排版只在 `styles/markdown.css` 一处定义，调用方的 `className` 只管布局，不写字号、
 * 行高与块间距（与流式的 `MarkdownText` 同一规则）。
 *
 * 链接与行内代码总是用 `MARKDOWN_LINK_COMPONENTS`，交给外层 `MarkdownLinkScope` 的打开器。调用方另给的
 * `components`（如文件预览的图片）必须身份恒定、会变的数据从 context 读：Streamdown 最外层的缓存比较不看
 * `components`，换了组件实例也不会重渲染。
 */
export function StaticMarkdown({ children, components, className }: {
  children: string;
  components?: StreamdownProps["components"];
  className?: string;
}) {
  const mermaidOptions = useMermaidOptions();
  const elements = useMemo(() => ({ ...MARKDOWN_LINK_COMPONENTS, ...components }), [components]);
  return (
    <Streamdown
      mode="static"
      plugins={markdownPlugins}
      rehypePlugins={staticRehypePlugins}
      mermaid={mermaidOptions}
      components={elements}
      className={cn("aui-md", className)}
    >
      {children}
    </Streamdown>
  );
}
