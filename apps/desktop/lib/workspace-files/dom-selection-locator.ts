import type { LineRange } from "@/lib/links/resolve-link";

/** 渲染视图 / PDF 文本层里的一次选区：正文与 DOM Range（片段渲染后仍用它求源码定位）。 */
export type DomTextSelection = { text: string; range: Range };

/** 选区起点、终点最近的有源码行号的块级元素。 */
function sourceLineBlock(node: Node, host: HTMLElement): HTMLElement | null {
  const element = node instanceof HTMLElement ? node : node.parentElement;
  const block = element?.closest<HTMLElement>("[data-source-line]") ?? null;
  return block && host.contains(block) ? block : null;
}

/**
 * 渲染视图（Markdown）选区对应的源码行范围。
 *
 * 渲染时每个块级元素都带 `data-source-line` / `data-source-line-end`（见 `rehypeSourceLines`），
 * 选区跨块时取起点块的首行到终点块的末行。只做到块级精度：块内部分选中也会带上整块的行范围——
 * 正文是模型按路径重读的指针，多几行比丢行安全。选不出块（如流式渲染中的元素）时返回 null。
 */
export function sourceLinesOfRange(range: Range, host: HTMLElement): LineRange | null {
  const start = sourceLineBlock(range.startContainer, host);
  const end = sourceLineBlock(range.endContainer, host) ?? start;
  if (!start || !end) return null;
  const startLine = Number(start.dataset.sourceLine);
  const endLine = Number(end.dataset.sourceLineEnd ?? end.dataset.sourceLine);
  if (!Number.isFinite(startLine) || !Number.isFinite(endLine)) return null;
  return { start: startLine, end: Math.max(startLine, endLine) };
}

/** 选区所在的 PDF 页（起点所在页）；不在 PDF 页里时返回 null。 */
export function pdfPageOfRange(range: Range, host: HTMLElement): number | null {
  const element = range.startContainer instanceof HTMLElement
    ? range.startContainer
    : range.startContainer.parentElement;
  const page = element?.closest<HTMLElement>(".page[data-page-number]") ?? null;
  if (!page || !host.contains(page)) return null;
  const pageNumber = Number(page.dataset.pageNumber);
  return Number.isInteger(pageNumber) && pageNumber > 0 ? pageNumber : null;
}
