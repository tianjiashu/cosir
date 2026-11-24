/**
 * 流式 Markdown（`MarkdownText`）每个渲染块的源文登记。
 *
 * Streamdown 先把正文切成块再逐块渲染，块内没有源码位置；摘录要把选区换回 Markdown（完整选中的块取源文，
 * 见 `lib/assistant/quotes/selection-excerpt`），就由块组件在挂载时把块元素与它的源文登记在这里。
 * 用 WeakMap 而不是 DOM 属性：源文可能很长，不该复制进 DOM；元素卸载后条目随之回收。
 */
const sources = new WeakMap<Element, string>();

/** 块元素上的标记属性：选区换算只认带它的元素。 */
export const MARKDOWN_BLOCK_ATTRIBUTE = "data-md-block";

export function registerMarkdownBlock(element: Element, source: string): void {
  sources.set(element, source);
}

/** 块元素的源文；不是登记过的块（或已卸载重建）时为 undefined。 */
export function markdownBlockSource(element: Element): string | undefined {
  return sources.get(element);
}
