import { Streamdown, type StreamdownProps } from "streamdown";

import { markdownPlugins, markdownRehypePlugins, useMermaidOptions } from "@/components/markdown/markdown-rendering-config";
import { rehypeHeadingLines } from "@/lib/markdown-outline";

// 模块级单例：插件数组每次渲染都新建会让 Streamdown 重新解析整篇文档。
const staticRehypePlugins = [...markdownRehypePlugins, rehypeHeadingLines];

/**
 * 整篇一次渲染的 Markdown（文件预览、配置预览、Skill 说明、带胶囊的用户消息）；流式的助手消息走 `MarkdownText`。
 *
 * 解析插件、HTML 清洗、代码高亮主题与 Mermaid 配色只在这里装配。标题带 `data-source-line`（源码行号），
 * 阅读器的大纲跳转与编辑器切到预览时的定位都按它找标题。链接与图片等元素由调用方经 `components` 决定。
 */
export function StaticMarkdown({ children, components, className }: {
  children: string;
  components?: StreamdownProps["components"];
  className?: string;
}) {
  const mermaidOptions = useMermaidOptions();
  return (
    <Streamdown
      mode="static"
      plugins={markdownPlugins}
      rehypePlugins={staticRehypePlugins}
      shikiTheme={["github-light", "github-dark"]}
      mermaid={mermaidOptions}
      components={components}
      className={className}
    >
      {children}
    </Streamdown>
  );
}
