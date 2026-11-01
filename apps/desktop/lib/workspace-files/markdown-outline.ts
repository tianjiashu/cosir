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

/**
 * rehype 插件：给标题元素写上 `data-source-line`（源码行号），渲染视图据此滚动到大纲选中的标题。
 *
 * 须排在 HTML 清洗之后（清洗会丢掉不在白名单里的属性，但保留节点位置）。Streamdown 的 static 模式整篇
 * 一次解析，位置就是全文行号，与 `markdownOutline` 一致；标题组件把额外属性透传到 DOM。
 */
export function rehypeHeadingLines() {
  return (tree: HastRoot) => {
    const visit = (node: HastNodes) => {
      if (node.type === "element" && /^h[1-6]$/.test(node.tagName) && node.position)
        node.properties.dataSourceLine = node.position.start.line;
      if ("children" in node) node.children.forEach(visit);
    };
    visit(tree);
  };
}
