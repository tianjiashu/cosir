import type { IBuffer, IBufferCell, IBufferLine } from "@xterm/xterm";

/** 回滚区行数；缓冲到达「视口行数 + 它」即开始丢最早的行。预览与阅读器用同一容量，「还有 N 行」才与阅读器一致。 */
export const SCROLLBACK_ROWS = 5000;
/** 预览显示的末尾行数。 */
export const PREVIEW_LINES = 8;

/** `color` 是 ANSI 色号或 `#rrggbb`，null 为默认前景；色号到颜色的换算留到渲染时，主题切换不用重新解析。 */
export type OutputRun = { text: string; color: number | string | null; bold: boolean; dim: boolean };
export type OutputTail = {
  /** 末尾几行，每行是同样式的文字片段。 */
  lines: OutputRun[][];
  /** 预览之外还有多少行。 */
  hiddenLines: number;
};

/**
 * 已渲染输出的纯文本：读取 xterm 公共 buffer，折行接回一行、不触碰用户选区，没有 ANSI 与 `\r` 进度帧；
 * 回滚区丢掉的最早部分不在其中（与丢行提示一致）。
 */
export function bufferText(buffer: IBuffer): string {
  let text = "";
  const end = lastContentRow(buffer);
  for (let i = 0; i <= end; i++) {
    const line = buffer.getLine(i);
    if (!line) continue;
    if (i > 0 && !line.isWrapped) text += "\n";
    // 折行边界的空格属于原始行；只有真正换行的尾部空白才裁剪。
    const nextWrapped = buffer.getLine(i + 1)?.isWrapped ?? false;
    text += line.translateToString(!nextWrapped);
  }
  return text;
}

function lastContentRow(buffer: IBuffer): number {
  let end = buffer.length - 1;
  while (end > 0 && !buffer.getLine(end)?.translateToString(true)) end--;
  return end;
}

function cellColor(cell: IBufferCell): OutputRun["color"] {
  if (cell.isFgDefault()) return null;
  return cell.isFgRGB() ? `#${cell.getFgColor().toString(16).padStart(6, "0")}` : cell.getFgColor();
}

function appendRow(runs: OutputRun[], line: IBufferLine, cell: IBufferCell): void {
  for (let x = 0; x < line.length; x++) {
    const current = line.getCell(x, cell);
    // 宽字符的第二格宽度为 0，内容已在第一格。
    if (!current || current.getWidth() === 0) continue;
    const color = cellColor(current), bold = current.isBold() !== 0, dim = current.isDim() !== 0;
    const last = runs.at(-1);
    const text = current.getChars() || " ";
    if (last && last.color === color && last.bold === bold && last.dim === dim) last.text += text;
    else runs.push({ text, color, bold, dim });
  }
}

/** 逻辑行的末尾空格来自未写过的格子，不是输出内容。 */
function trimLineEnd(runs: OutputRun[]): OutputRun[] {
  while (runs.length > 0) {
    const last = runs[runs.length - 1];
    last.text = last.text.trimEnd();
    if (last.text) break;
    runs.pop();
  }
  return runs;
}

/** 已渲染输出末尾的几条逻辑行（折行接回），带颜色与粗细。 */
export function tailLines(buffer: IBuffer): OutputTail {
  const starts: number[] = [];
  const end = lastContentRow(buffer);
  for (let i = 0; i <= end; i++) if (!buffer.getLine(i)?.isWrapped) starts.push(i);
  const shown = starts.slice(-PREVIEW_LINES);
  const cell = buffer.getNullCell();
  const lines = shown.map((start, index) => {
    const runs: OutputRun[] = [];
    const next = shown[index + 1] ?? end + 1;
    for (let row = start; row < next; row++) appendRow(runs, buffer.getLine(row)!, cell);
    return trimLineEnd(runs);
  });
  return { lines, hiddenLines: starts.length - shown.length };
}
