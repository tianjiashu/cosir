/**
 * 把 `.mmd` / `.mermaid` 文件的内容包成一篇只有一个 mermaid 代码块的 Markdown，交给现有的 Markdown 渲染
 * （主题、下载、缩放都复用）。围栏用比正文里最长的反引号串多一个的长度，正文里出现 ``` 也不会提前闭合。
 */
export function mermaidDocument(source: string): string {
  const longest = Math.max(0, ...Array.from(source.matchAll(/`+/g), (match) => match[0].length));
  const fence = "`".repeat(Math.max(3, longest + 1));
  return `${fence}mermaid\n${source.replace(/\n$/, "")}\n${fence}\n`;
}
