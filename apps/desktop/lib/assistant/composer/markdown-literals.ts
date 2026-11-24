import { fromMarkdown } from "mdast-util-from-markdown";
import type { Root, RootContent } from "mdast";

/**
 * 不识别 HTML 块（与后端 `Capsule._parser` 一致）：胶囊标签长得像 HTML，独占一行时会被当成 HTML 块，紧跟着的
 * 围栏就不再是代码——摘录卡片排在正文开头（`quote-cards`），正文以代码开头时正是这样。两端规则不同会让同一段正文
 * 前端认出胶囊、后端当字面量。行内 HTML 照常识别：它让标签属性里没转义的反引号不被当成行内代码（宽容读入）。
 */
const WITHOUT_HTML_BLOCKS = [{ disable: { null: ["htmlFlow"] } }];

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
  visit(fromMarkdown(text, { extensions: WITHOUT_HTML_BLOCKS }));
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
export type MarkdownTokenPart<T> = { type: "text"; text: string } | { type: "token"; token: T };

/**
 * 按 token 正则把正文切成文本与 token 片段；代码块、行内代码里的同形文本不识别。
 * `toToken` 返回 null 的匹配按原文保留；相邻文本合并，不产出空文本片段。
 */
export function splitMarkdownTokens<T>(
  text: string,
  pattern: RegExp,
  toToken: (match: RegExpMatchArray) => T | null,
): MarkdownTokenPart<T>[] {
  const parts: MarkdownTokenPart<T>[] = [];
  const appendText = (value: string) => {
    if (!value) return;
    const last = parts.at(-1);
    if (last?.type === "text") last.text += value;
    else parts.push({ type: "text", text: value });
  };
  for (const segment of markdownSegments(text)) {
    if (segment.literal) {
      appendText(segment.text);
      continue;
    }
    let cursor = 0;
    for (const match of segment.text.matchAll(pattern)) {
      const start = match.index ?? cursor;
      const token = toToken(match);
      appendText(segment.text.slice(cursor, token === null ? start + match[0].length : start));
      if (token !== null) parts.push({ type: "token", token });
      cursor = start + match[0].length;
    }
    appendText(segment.text.slice(cursor));
  }
  return parts;
}

export const mapMarkdownText = (
  text: string,
  transform: (text: string) => string,
) =>
  markdownSegments(text)
    .map((part) => (part.literal ? part.text : transform(part.text)))
    .join("");
