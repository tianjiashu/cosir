import { fromMarkdown } from "mdast-util-from-markdown";
import type { Nodes as MdastNodes } from "mdast";
import type { Nodes as HastNodes, Root as HastRoot } from "hast";

/** 大纲里的一个标题；`line` 是它在阅读器当前文本里的行号（1 起），两种视图都按它定位。 */
export type OutlineHeading = { depth: number; text: string; line: number };

function plainText(node: MdastNodes): string {
  if ("value" in node) return node.value;
  return "children" in node ? node.children.map(plainText).join("") : "";
}

/**
 * 从 Markdown 文本派生大纲：列出全部标题（含列表、引用里的），保留层级与源码行号。
 *
 * 大纲不是事实，随文本即时算出；源码视图按 `line` 选中整行，渲染视图按 `rehypeHeadingLines` 打在标题
 * 元素上的同一个行号滚动过去，所以两边不需要另一套标题 id。
 */
export function markdownOutline(text: string): OutlineHeading[] {
  const headings: OutlineHeading[] = [];
  const visit = (node: MdastNodes) => {
    if (node.type === "heading" && node.position)
      headings.push({ depth: node.depth, text: plainText(node).trim() || "（空标题）", line: node.position.start.line });
    else if ("children" in node) node.children.forEach(visit);
  };
  visit(fromMarkdown(text));
  return headings;
}

/** 带 `data-source-line` 的块级元素：选区的块级定位只认这些（行内元素不单独标）。 */
const SOURCE_LINE_BLOCKS = new Set([
  "p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "li", "table", "thead",
  "tbody", "tr", "td", "th", "hr",
]);

/**
 * rehype 插件：给块级元素写上源码行号（`data-source-line` 起始行、`data-source-line-end` 结束行）。
 *
 * 文件阅读器的大纲跳转、Markdown 编辑器切到预览时的定位按标题的 `data-source-line` 找；渲染视图里
 * 「添加选区到对话」按选区两端最近的块级元素的这两个属性求源码行范围（见 `sourceLinesOfRange`）。
 * 由 `StaticMarkdown` 统一装配。
 *
 * 须排在 HTML 清洗之后（清洗会丢掉不在白名单里的属性，但保留节点位置）。Streamdown 的 static 模式整篇
 * 一次解析，位置就是全文行号，与 `markdownOutline` 一致；组件把额外属性透传到 DOM。
 */
export function rehypeSourceLines() {
  return (tree: HastRoot) => {
    const visit = (node: HastNodes) => {
      if (node.type === "element" && SOURCE_LINE_BLOCKS.has(node.tagName) && node.position) {
        node.properties.dataSourceLine = node.position.start.line;
        node.properties.dataSourceLineEnd = node.position.end.line;
      }
      if ("children" in node) node.children.forEach(visit);
    };
    visit(tree);
  };
}
