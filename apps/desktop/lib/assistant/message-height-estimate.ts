import type { ThreadMessage } from "@assistant-ui/react";

/**
 * 按消息内容粗估虚拟列表行高（px，含行间距），只作首次测量前的初始值。
 *
 * 估计越接近真实高度，行首次挂载时虚拟列表对 scrollTop 的修正就越小：向上滚动时每条新挂载的行
 * 都会按「真实 − 估计」补偿滚动位置，固定常量（曾为 120px，而回答常见 700px 以上）会让补偿量
 * 达到数百像素，WKWebView 的惯性滚动会因此被打断。真实高度仍以测量为准，这里只逐行区分 Markdown
 * 的块类型（标题、列表、表格、代码、正文）与用户附件；工具卡片、思考过程按折叠态计，展开的卡片、
 * 公式与 Mermaid 不建模。常量用单一特性的消息在 `max-w-3xl` 对话列宽下逐项实测得出。
 */
export function estimateMessageHeight(message: ThreadMessage | undefined): number {
  if (!message) return ROW_GAP + ASSISTANT_CHROME;
  // 消息对象按版本不可变，未变的消息不必在每次布局重算时重新扫描正文。
  let height = estimates.get(message);
  if (height === undefined) {
    height = Math.round(message.role === "user" ? userHeight(message) : assistantHeight(message));
    estimates.set(message, height);
  }
  return height;
}

const estimates = new WeakMap<ThreadMessage, number>();

// 行容器 `pb-6`。
const ROW_GAP = 24;

// 用户消息：附件区在气泡上方右对齐、可折行；气泡最宽为列宽的 85%。
const USER_CHROME = 29;
const USER_BUBBLE_PADDING = 20;
const USER_LINE_HEIGHT = 23;
const USER_LINE_UNITS = 44;
const ATTACHMENT_GAP = 8;
const IMAGE_HEIGHT = 130;
const IMAGES_PER_ROW = 3;
const FILE_CHIP_HEIGHT = 30;
const FILES_PER_ROW = 3;

function userHeight(message: ThreadMessage): number {
  let text = "";
  let images = 0;
  let files = 0;
  for (const part of message.content) {
    if (part.type === "text") text += (text ? "\n" : "") + part.text;
    else if (part.type === "image") images += 1;
    else if (part.type === "file") files += 1;
  }
  const blocks: number[] = [];
  if (images > 0) blocks.push(Math.ceil(images / IMAGES_PER_ROW) * (IMAGE_HEIGHT + ATTACHMENT_GAP) - ATTACHMENT_GAP);
  if (files > 0) blocks.push(Math.ceil(files / FILES_PER_ROW) * (FILE_CHIP_HEIGHT + ATTACHMENT_GAP) - ATTACHMENT_GAP);
  if (text.trim()) blocks.push(USER_BUBBLE_PADDING + textLines(text, USER_LINE_UNITS) * USER_LINE_HEIGHT);
  return ROW_GAP + USER_CHROME + blocks.reduce((sum, block) => sum + block, 0) + Math.max(0, blocks.length - 1) * ATTACHMENT_GAP;
}

// 回答：底部操作栏与留白；各 part 之间的间距。相邻的思考过程与工具调用合成一组折叠的追踪行
// （`assistantMessageGroupBy`），组内每多一项多一行。
const ASSISTANT_CHROME = 28;
const PART_GAP = 12;
const TRACE_GROUP = 36;
const TRACE_ITEM = 28;

function assistantHeight(message: ThreadMessage): number {
  let height = ROW_GAP + ASSISTANT_CHROME;
  let inTrace = false;
  for (const part of message.content) {
    const trace = part.type === "reasoning" || part.type === "tool-call";
    if (trace) height += inTrace ? TRACE_ITEM : TRACE_GROUP + PART_GAP;
    else if (part.type === "text") height += markdownHeight(part.text) + PART_GAP;
    inTrace = trace;
  }
  return height;
}

// Markdown（`.aui-md`，块间距 `space-y-3`）。标题按级别取行高，含上下外边距。
const BLOCK_GAP = 12;
const PROSE_LINE_HEIGHT = 24;
const PROSE_LINE_UNITS = 46;
const HEADING_HEIGHTS = [36, 32, 28, 24, 24, 24];
const HEADING = /^\s*(#{1,6})\s/;
// 列表项自带上下间距，比正文行高一档。
const LIST_ITEM_HEIGHT = 32;
const LIST_ITEM = /^\s*(?:[-*+]|\d+[.)])\s/;
const TABLE_ROW_HEIGHT = 37;
const TABLE_CHROME = 3;
const TABLE_ROW = /^\s*\|/;
const TABLE_DIVIDER = /^\s*\|?\s*:?-{3,}/;
const CODE_LINE_HEIGHT = 20.5;
const CODE_BLOCK_CHROME = 62;

function markdownHeight(text: string): number {
  let height = 0;
  let codeLines = -1;
  let tableRows = 0;
  const closeTable = () => {
    if (tableRows > 0) height += TABLE_CHROME + tableRows * TABLE_ROW_HEIGHT;
    tableRows = 0;
  };
  for (const block of text.split(/\n{2,}/)) {
    for (const line of block.split("\n")) {
      if (line.trimStart().startsWith("```")) {
        if (codeLines < 0) codeLines = 0;
        else {
          height += CODE_BLOCK_CHROME + codeLines * CODE_LINE_HEIGHT;
          codeLines = -1;
        }
        continue;
      }
      if (codeLines >= 0) {
        codeLines += 1;
        continue;
      }
      if (TABLE_ROW.test(line)) {
        if (!TABLE_DIVIDER.test(line)) tableRows += 1;
        continue;
      }
      closeTable();
      const heading = HEADING.exec(line);
      if (heading) height += HEADING_HEIGHTS[heading[1].length - 1];
      else if (LIST_ITEM.test(line)) height += LIST_ITEM_HEIGHT + (textLines(line, PROSE_LINE_UNITS) - 1) * PROSE_LINE_HEIGHT;
      else height += textLines(line, PROSE_LINE_UNITS) * PROSE_LINE_HEIGHT;
    }
    closeTable();
    if (codeLines < 0) height += BLOCK_GAP;
  }
  // 流式中未闭合的代码块。
  if (codeLines > 0) height += CODE_BLOCK_CHROME + codeLines * CODE_LINE_HEIGHT;
  return height;
}

/** 折行后的行数：全角字符计 1 个宽度单位，其余计 0.55。空行算一行。 */
function textLines(text: string, unitsPerLine: number): number {
  let lines = 0;
  for (const line of text.split("\n")) {
    let units = 0;
    for (const char of line) units += char.charCodeAt(0) > 0x2e7f ? 1 : 0.55;
    lines += Math.max(1, Math.ceil(units / unitsPerLine));
  }
  return lines;
}
