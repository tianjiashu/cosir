import { useMemo } from "react";

/**
 * 按搜索模式高亮命中行里的片段。
 *
 * 后端用 Python `re` 搜索，前端用 JS 正则近似复现：两者常用语法一致，遇到 JS 不支持的
 * 写法（如 `(?P<name>)`、行内 flag）编译失败时原样展示整行，不做语法翻译——高亮只是
 * 阅读辅助，命中与否已由后端决定。
 */
export function HighlightedMatch({ text, pattern }: { text: string; pattern: string }) {
  const segments = useMemo(() => splitByPattern(text, pattern), [text, pattern]);
  return (
    <>
      {segments.map((segment, index) => segment.match
        ? <mark key={index} className="rounded-sm bg-warning/25 text-foreground">{segment.text}</mark>
        : <span key={index}>{segment.text}</span>)}
    </>
  );
}

function splitByPattern(text: string, pattern: string): { text: string; match: boolean }[] {
  if (!pattern) return [{ text, match: false }];
  let regex: RegExp;
  try {
    regex = new RegExp(pattern, "g");
  } catch {
    return [{ text, match: false }];
  }
  const segments: { text: string; match: boolean }[] = [];
  let cursor = 0;
  for (const found of text.matchAll(regex)) {
    // 空匹配（如 `^`、`a*`）没有可高亮的内容，跳过即可；matchAll 自身会推进位置。
    if (found[0] === "") continue;
    const start = found.index;
    if (start > cursor) segments.push({ text: text.slice(cursor, start), match: false });
    segments.push({ text: found[0], match: true });
    cursor = start + found[0].length;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), match: false });
  return segments;
}
