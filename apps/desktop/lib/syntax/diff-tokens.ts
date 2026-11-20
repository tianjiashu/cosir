import { markEdits, pickRanges, tokenize, type ChangeData, type HunkData, type HunkTokens, type RangeTokenNode, type TokenizeEnhancer } from "react-diff-view";

import type { LanguageHighlighter } from "./highlighter";

/** 着色区间：`style` 是词元的浅色与深色（`color`、`--shiki-dark`）。 */
export type SyntaxRange = RangeTokenNode & { properties: { style: Record<string, string> } };

type SideLine = { lineNumber: number; content: string };

/**
 * 一个文件 diff 的高亮数据（交给 react-diff-view 的 `tokens`）：语法着色（语言就绪时）叠上字级改动。
 *
 * 按段计算：react-diff-view 的 `tokenize` 按最大行号建满数组，改动落在大文件末尾时（第 20 万行约 0.4 秒）
 * 展开就卡住主线程，成本跟行号走而不是跟改动走。所以每段改动先把行号平移到从 1 开始单独算，再按真实行号
 * 放回稀疏数组；渲染按行号取 token，没有改动的行取不到就按原文显示。
 */
export function diffTokens(hunks: HunkData[], highlighter: LanguageHighlighter | null): HunkTokens {
  const tokens: HunkTokens = { old: [], new: [] };
  for (const hunk of hunks) {
    const oldBase = Math.max(hunk.oldStart - 1, 0);
    const newBase = Math.max(hunk.newStart - 1, 0);
    const local = [shiftHunk(hunk, oldBase, newBase)];
    const enhancers: TokenizeEnhancer[] = [];
    if (highlighter) enhancers.push(pickRanges(...diffSyntaxRanges(local, highlighter)));
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
 * 给 diff 两侧算语法着色区间，交给 react-diff-view 的 `pickRanges`；颜色来自着色服务（与代码块、编辑器同源）。
 *
 * 只有补丁里的几段代码、没有全文：每段改动的旧侧（未改 + 删除）与新侧（未改 + 新增）各自拼成一段文本单独着色，
 * 段与段之间不相连。段首正好落在块注释、多行字符串中间时这一段会上错色，范围只限这一段，可接受。
 * 行号是文件里的真实行号（与 `markEdits` 一致），列从 0 起。区间带着词元的浅色与深色（`properties.style`），
 * 由 diff 视图的 `renderToken` 画出。
 */
export function diffSyntaxRanges(hunks: HunkData[], highlighter: LanguageHighlighter): [old: SyntaxRange[], new: SyntaxRange[]] {
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
    oldRanges.push(...sideRanges(oldLines, highlighter));
    newRanges.push(...sideRanges(newLines, highlighter));
  }
  return [oldRanges, newRanges];
}

function sideRanges(lines: SideLine[], highlighter: LanguageHighlighter): SyntaxRange[] {
  if (lines.length === 0) return [];
  const colored = highlighter.codeTokens(lines.map((line) => line.content).join("\n")).tokens;
  return lines.flatMap((line, index) => {
    let start = 0;
    return colored[index].map((token) => {
      const range = { type: "syntax", lineNumber: line.lineNumber, start, length: token.content.length, properties: { style: token.htmlStyle ?? {} } };
      start += token.content.length;
      return range;
    });
  });
}
