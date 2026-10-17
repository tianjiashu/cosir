import { fromMarkdown } from "mdast-util-from-markdown";
import type { Root, RootContent } from "mdast";

/** 用户代码是原文，不能把其中的附件或 Skill 示例激活。转换只作用于代码外文本。 */
export function markdownSegments(
  text: string,
): Array<{ text: string; literal: boolean }> {
  if (
    !text.includes("`") &&
    !text.includes("~") &&
    !text.includes("    ") &&
    !text.includes("\t")
  )
    return [{ text, literal: false }];
  const ranges: Array<[number, number]> = [];
  const visit = (node: Root | RootContent) => {
    if (node.type === "code" || node.type === "inlineCode") {
      ranges.push([node.position!.start.offset!, node.position!.end.offset!]);
      return;
    }
    if ("children" in node)
      for (const child of node.children) visit(child as RootContent);
  };
  visit(fromMarkdown(text));
  const result: Array<{ text: string; literal: boolean }> = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start > cursor)
      result.push({ text: text.slice(cursor, start), literal: false });
    result.push({ text: text.slice(start, end), literal: true });
    cursor = end;
  }
  if (cursor < text.length)
    result.push({ text: text.slice(cursor), literal: false });
  return result;
}
export const mapMarkdownText = (
  text: string,
  transform: (text: string) => string,
) =>
  markdownSegments(text)
    .map((part) => (part.literal ? part.text : transform(part.text)))
    .join("");
