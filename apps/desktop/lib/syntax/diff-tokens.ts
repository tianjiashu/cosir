import type { Language } from "@codemirror/language";
import { highlightTree } from "@lezer/highlight";
import { markEdits, pickRanges, tokenize, type ChangeData, type HunkData, type HunkTokens, type RangeTokenNode, type TokenizeEnhancer } from "react-diff-view";

import { syntaxHighlighter } from "./syntax-highlighter";

/** react-diff-view 的区间：默认渲染把 `properties.className` 写到 span 上。 */
export type SyntaxRange = RangeTokenNode & { properties: { className: string } };

type SideLine = { lineNumber: number; content: string };

type Parser = Language["parser"];

/**
 * 一个文件 diff 的高亮数据（交给 react-diff-view 的 `tokens`）：语法高亮（`parser` 给出时）叠上字级改动。
 *
 * 按段计算：react-diff-view 的 `tokenize` 按最大行号建满数组，改动落在大文件末尾时（第 20 万行约 0.4 秒）
 * 展开就卡住主线程，成本跟行号走而不是跟改动走。所以每段改动先把行号平移到从 1 开始单独算，再按真实行号
 * 放回稀疏数组；渲染按行号取 token，没有改动的行取不到就按原文显示。
 */
export function diffTokens(hunks: HunkData[], parser: Parser | null): HunkTokens {
  const tokens: HunkTokens = { old: [], new: [] };
  for (const hunk of hunks) {
    const oldBase = Math.max(hunk.oldStart - 1, 0);
    const newBase = Math.max(hunk.newStart - 1, 0);
    const local = [shiftHunk(hunk, oldBase, newBase)];
    const enhancers: TokenizeEnhancer[] = [];
    if (parser) enhancers.push(pickRanges(...diffSyntaxRanges(local, parser)));
    enhancers.push(markEdits(local, { type: "block" }));
    const part = tokenize(local, { enhancers });
    part.old.forEach((line, index) => { tokens.old[oldBase + index] = line; });
    part.new.forEach((line, index) => { tokens.new[newBase + index] = line; });
  }
  return tokens;
}

function shiftHunk(hunk: HunkData, oldBase: number, newBase: number): HunkData {
  const changes = hunk.changes.map((change): ChangeData => change.type === "normal"
    ? { ...change, oldLineNumber: change.oldLineNumber - oldBase, newLineNumber: change.newLineNumber - newBase }
    : { ...change, lineNumber: change.lineNumber - (change.type === "delete" ? oldBase : newBase) });
  return { ...hunk, oldStart: hunk.oldStart - oldBase, newStart: hunk.newStart - newBase, changes };
}

/**
 * 给 diff 两侧算语法高亮区间，交给 react-diff-view 的 `pickRanges`。
 *
 * 只有补丁里的几段代码、没有全文：每段改动的旧侧（未改 + 删除）与新侧（未改 + 新增）各自拼成一段文本单独解析，
 * 段与段之间不相连。段首正好落在块注释、多行字符串中间时这一段会上错色，范围只限这一段，可接受。
 * 行号是文件里的真实行号（与 `markEdits` 一致），列从 0 起；跨行的语法区间按行切开。
 */
export function diffSyntaxRanges(hunks: HunkData[], parser: Parser): [old: SyntaxRange[], new: SyntaxRange[]] {
  const oldRanges: SyntaxRange[] = [];
  const newRanges: SyntaxRange[] = [];
  for (const hunk of hunks) {
    const oldLines: SideLine[] = [];
    const newLines: SideLine[] = [];
    for (const change of hunk.changes) {
      if (change.type === "normal") {
        oldLines.push({ lineNumber: change.oldLineNumber, content: change.content });
        newLines.push({ lineNumber: change.newLineNumber, content: change.content });
      } else if (change.type === "delete") oldLines.push({ lineNumber: change.lineNumber, content: change.content });
      else newLines.push({ lineNumber: change.lineNumber, content: change.content });
    }
    oldRanges.push(...sideRanges(oldLines, parser));
    newRanges.push(...sideRanges(newLines, parser));
  }
  return [oldRanges, newRanges];
}

function sideRanges(lines: SideLine[], parser: Parser): SyntaxRange[] {
  if (lines.length === 0) return [];
  const starts: number[] = [];
  let offset = 0;
  for (const line of lines) {
    starts.push(offset);
    offset += line.content.length + 1;
  }
  const ranges: SyntaxRange[] = [];
  highlightTree(parser.parse(lines.map((line) => line.content).join("\n")), syntaxHighlighter, (from, to, className) => {
    let index = lineIndexAt(starts, from);
    while (from < to && index < lines.length) {
      const lineEnd = starts[index] + lines[index].content.length;
      const end = Math.min(to, lineEnd);
      if (end > from) ranges.push({ type: "syntax", lineNumber: lines[index].lineNumber, start: from - starts[index], length: end - from, properties: { className } });
      index += 1;
      from = starts[index] ?? to;
    }
  });
  return ranges;
}

/** `starts` 递增：最后一个不大于 `offset` 的行。 */
function lineIndexAt(starts: number[], offset: number): number {
  let low = 0;
  let high = starts.length - 1;
  while (low < high) {
    const middle = (low + high + 1) >> 1;
    if (starts[middle] <= offset) low = middle;
    else high = middle - 1;
  }
  return low;
}
