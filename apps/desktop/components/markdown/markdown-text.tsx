import { StreamdownTextPrimitive } from "@assistant-ui/react-streamdown";

import { useMarkdownLinkComponents } from "@/components/links/markdown-links";
import { markdownPlugins, markdownRehypePlugins, useMermaidOptions } from "@/components/markdown/markdown-rendering-config";

/**
 * 流式正文逐字淡入。必须按字切：默认按空白切词，一整句中文会被当成一个单位。模块级常量是因为 Streamdown
 * 按引用比较这一项，内联对象会让每次渲染都穿透它的 memo。字级 span 只在该 part 运行中存在，结束时一次收回。
 */
const STREAM_REVEAL = { animation: "fadeIn", duration: 160, sep: "char", stagger: 8 } as const;

/**
 * Assistant 正文和 reasoning 的唯一 Markdown 边界。
 * Streamdown 负责不完整 Markdown、代码高亮、公式、Mermaid 图表、CJK 标点与流式淡入；
 * 组件本身不解析内容，也不把渲染结果写回 runtime 或服务端。链接与行内代码里的工作区路径
 * 经 `useMarkdownLinkComponents` 识别、由链接打开入口统一打开（所属 Task 来自 `LinkTaskContext`）。
 */
export function MarkdownText() {
  const mermaidOptions = useMermaidOptions();
  const components = useMarkdownLinkComponents("message", { icons: true });
  return (
    <StreamdownTextPrimitive
      mode="streaming"
      defer
      plugins={markdownPlugins}
      rehypePlugins={markdownRehypePlugins}
      shikiTheme={["github-light", "github-dark"]}
      animated={STREAM_REVEAL}
      mermaid={mermaidOptions}
      components={components}
      className="aui-md !space-y-3 leading-6"
    />
  );
}
