import type { QuoteCapsule, WorkspaceLocator } from "@/lib/assistant/capsules";
import { baseName, fileExtension } from "@/lib/files/file-name";
import type { LineRange } from "@/lib/links/resolve-link";
import { markdownExcerpt, truncateExcerpt } from "@/lib/text-selection/markdown-excerpt";

/**
 * 文件阅读器的选区 → 摘录（带工作区位置）的规则：Markdown 渲染视图、源码视图与 PDF 各一个入口，位置规则一处。
 *
 * 位置只在能说准时给：分段读取的大文件只有从开头读的那一段（offset 0）行号与整篇一致，其余段只给路径
 * （`wholeFileLines`，与旧的 `<ref>` 选区同一规则）；PDF 给起点所在页。
 */
export function fileSelectionQuote(path: string, text: string | null, locator: WorkspaceLocator): QuoteCapsule | null {
  return text?.trim() ? { kind: "quote", path, ...locator, text: truncateExcerpt(text) } : null;
}

/** 行范围定位：只有从开头读的那一段（offset 0）能给。 */
export function wholeFileLines(pageOffset: number, lines: LineRange | null): WorkspaceLocator {
  if (pageOffset !== 0 || lines === null) return {};
  return { lines: lines.start === lines.end ? String(lines.start) : `${lines.start}-${lines.end}` };
}

/** 源码视图的选中文字包成带语言的代码块（摘录条与气泡按 Markdown 渲染原文）。 */
export function sourceExcerpt(path: string, text: string): string {
  return `\`\`\`${fileExtension(baseName(path))?.toLowerCase() ?? ""}\n${text.replace(/\n$/, "")}\n\`\`\``;
}

/**
 * Markdown 渲染视图的选区换回源文：块是最外层带 `data-source-line` 的元素（`rehypeSourceLines`），整块选中时
 * 按行号切这一段原文，没选全按类型截取（见 `markdownExcerpt`）。
 */
export function renderedMarkdownExcerpt(range: Range, host: HTMLElement, source: string): string | null {
  const lines = source.split("\n");
  const blocks = [...host.querySelectorAll<HTMLElement>("[data-source-line]")]
    .filter((block) => !block.parentElement?.closest("[data-source-line]"));
  return markdownExcerpt(range, {
    blocks,
    sourceOf: (block) => {
      const start = Number(block.dataset.sourceLine);
      const end = Number(block.dataset.sourceLineEnd ?? start);
      return Number.isFinite(start) && Number.isFinite(end) ? lines.slice(start - 1, end).join("\n") : undefined;
    },
  });
}
