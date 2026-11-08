import type { TransportOutcome } from "./contract";
import { resolveDisclosure, processKey, activityKey, agentKey } from "./disclosure-store";
import type { ThreadMessage } from "@assistant-ui/react";
import { IMAGE_TILE_GAP, IMAGE_TILE_SIZE } from "@/lib/image/tile-size";
import { IMAGES_PER_ROW, shouldStackImages } from "./image-stack-policy";
import { visibleTurnBlock, type TurnLayout } from "./turn-layout";
import { projectTurnHeading, TURN_HEADING_HEIGHT } from "./turn-heading";

/**
 * 按消息内容粗估虚拟列表行高（px，含行间距），只作首次测量前的初始值。
 *
 * 估计越接近真实高度，行首次挂载时虚拟列表对 scrollTop 的修正就越小：向上滚动时每条新挂载的行
 * 都会按「真实 − 估计」补偿滚动位置，固定常量（曾为 120px，而回答常见 700px 以上）会让补偿量
 * 达到数百像素，惯性滚动会因此被打断（最初在已移除的 WKWebView 宿主上观察到）。真实高度仍以测量为准，这里只逐行区分 Markdown
 * 的块类型（标题、列表、表格、代码、正文）与用户附件；过程区和工具组读取用户开合覆盖（未传 Task 时
 * 按默认值计），调用行展开体、公式与 Mermaid 不建模。正文常量用单一特性的消息在 `max-w-3xl` 对话列宽下逐项
 * 实测得出。
 */
export function estimateMessageHeight(message: ThreadMessage | undefined, taskId?: number): number {
  if (!message) return ROW_GAP + ASSISTANT_CHROME;
  // 内容计算按不可变消息版本缓存；开合仍实时读取，不能缓存依赖 Task 偏好的最终高度。
  return Math.round(message.role !== "user" ? assistantHeight(message, taskId)
    : isAgentMessage(message) ? ROW_GAP + agentRowHeight(message.id, message.content.map((part) => part.type === "text" ? part.text : "").join("\n"), taskId)
      : userHeight(message));
}

const userHeights = new WeakMap<ThreadMessage, number>();
const assistantEstimates = new WeakMap<ThreadMessage, {
  layout: TurnLayout;
  hasHeading: boolean;
  proseHeights: Map<number, number>;
}>();

// 行容器 `pb-6`。
const ROW_GAP = 24;

// 用户消息：附件区在气泡上方右对齐、可折行；气泡最宽为列宽的 85%。
const USER_CHROME = 29;
const USER_BUBBLE_PADDING = 24;
const USER_LINE_HEIGHT = 23;
const USER_LINE_UNITS = 44;
const ATTACHMENT_GAP = IMAGE_TILE_GAP;
const IMAGE_HEIGHT = IMAGE_TILE_SIZE.regular.height;
// 超过阈值时收成一叠（`ImageStack`）：错开的两层是 transform，不占布局高度，收起态与单张图同高。
const IMAGE_STACK_HEIGHT = IMAGE_HEIGHT;
const FILE_CHIP_HEIGHT = 30;
const FILES_PER_ROW = 3;

function userHeight(message: ThreadMessage): number {
  const cached = userHeights.get(message);
  if (cached !== undefined) return cached;
  let text = "";
  let images = 0;
  let files = 0;
  for (const part of message.content) {
    if (part.type === "text") text += (text ? "\n" : "") + part.text;
    else if (part.type === "image") images += 1;
    else if (part.type === "file") files += 1;
  }
  const blocks: number[] = [];
  if (shouldStackImages(images)) blocks.push(IMAGE_STACK_HEIGHT);
  else if (images > 0) blocks.push(Math.ceil(images / IMAGES_PER_ROW) * (IMAGE_HEIGHT + ATTACHMENT_GAP) - ATTACHMENT_GAP);
  if (files > 0) blocks.push(Math.ceil(files / FILES_PER_ROW) * (FILE_CHIP_HEIGHT + ATTACHMENT_GAP) - ATTACHMENT_GAP);
  if (text.trim()) blocks.push(USER_BUBBLE_PADDING + textLines(text, USER_LINE_UNITS) * USER_LINE_HEIGHT);
  const height = ROW_GAP + USER_CHROME + blocks.reduce((sum, block) => sum + block, 0) + Math.max(0, blocks.length - 1) * ATTACHMENT_GAP;
  userHeights.set(message, height);
  return height;
}

// Agent 消息（`AgentInboundMessage`）：一行来源，展开后正文按 Markdown 排版；常量按
// 样式类推算，未单独实测。
const AGENT_LABEL = 22;
const AGENT_PANEL_PADDING = 20;

function isAgentMessage(message: ThreadMessage): boolean {
  return Boolean((message.metadata?.custom as { source?: unknown } | undefined)?.source);
}

/** 来信默认收起，展开的才解析正文，不做缓存。不含行间距：独立行与回合内的片段间距不同。 */
function agentRowHeight(id: string, text: string, taskId?: number): number {
  const open = taskId !== undefined && resolveDisclosure(taskId, agentKey(id), false);
  return AGENT_LABEL + (open ? AGENT_PANEL_PADDING + markdownHeight(text) : 0);
}

// 回答：脚注（操作栏及其前面的区间距）与留白；各区之间的间距。回合按 `projectTurn` 分区计高（规则与渲染
// 同源，见 `turn-layout.ts`）：过程默认按 outcome 折叠、运行中保留正文及活动摘要；等待卡与产物卡常显；正文按
// Markdown 逐块计。正文与脚注常量按 Chromium 的对话列宽实测校准，
// 区卡常量仍是按设计稿（§5.9）取的预估值。
const ASSISTANT_CHROME = 52;
const ZONE_GAP = 16;
const PROCESS_HEADER = 33;
const PROCESS_ITEM = 28;
const ATTENTION_CARD = 96;
const CHANGES_CARD_CHROME = 40;
const CHANGES_FILE_ROW = 28;
const STANDALONE_ARTIFACT_CARD = 120;

function assistantHeight(message: ThreadMessage, taskId?: number): number {
  let estimate = assistantEstimates.get(message);
  if (!estimate) {
    const outcome = (message.metadata?.custom?.outcome ?? null) as TransportOutcome | null;
    const layout = message.metadata.custom.turnLayout as TurnLayout;
    estimate = { layout, hasHeading: projectTurnHeading(layout, outcome) !== null, proseHeights: new Map() };
    assistantEstimates.set(message, estimate);
  }
  const { layout, hasHeading, proseHeights } = estimate;
  const open = taskId === undefined ? layout.defaultProcessOpen : resolveDisclosure(taskId, processKey(message.id), layout.defaultProcessOpen);
  const blocks: number[] = [];
  if (hasHeading) blocks.push(TURN_HEADING_HEIGHT);
  for (const block of layout.blocks) {
    if (!visibleTurnBlock(block, open, layout.current?.groupKey)) continue;
    if (block.kind === "narration") {
      let height = proseHeights.get(block.index);
      if (height === undefined) {
        height = markdownHeight(block.part.text);
        proseHeights.set(block.index, height);
      }
      blocks.push(height);
    } else if (block.kind === "agent") {
      blocks.push(agentRowHeight(block.part.id, block.part.text, taskId));
    } else if (block.kind === "standalone") {
      blocks.push(PROCESS_ITEM);
    } else if (block.calls.length || block.key === layout.current?.groupKey) {
      const groupOpen = taskId !== undefined && resolveDisclosure(taskId, activityKey(message.id, block.key), false);
      blocks.push(PROCESS_HEADER + (groupOpen ? block.calls.length : 0) * PROCESS_ITEM);
    }
  }
  for (let index = 0; index < layout.attention.length; index++) blocks.push(ATTENTION_CARD);
  for (const card of layout.artifacts) blocks.push(card.kind === "changes" ? CHANGES_CARD_CHROME + card.files.length * CHANGES_FILE_ROW : STANDALONE_ARTIFACT_CARD);
  if (layout.intro) return ROW_GAP + blocks.reduce((sum, block) => sum + block, 0) + Math.max(0, blocks.length - 1) * ZONE_GAP;
  return ROW_GAP + ASSISTANT_CHROME + blocks.reduce((sum, block) => sum + block + ZONE_GAP, 0);
}


// Markdown（`.aui-md`，块间距 `space-y-3`）。标题按级别取行高，含上下外边距。
const BLOCK_GAP = 12;
const PROSE_LINE_HEIGHT = 28;
const PROSE_LINE_UNITS = 46;
const HEADING_HEIGHTS = [36, 32, 28, 24, 24, 24];
const HEADING = /^\s*(#{1,6})\s/;
// 列表项自带上下间距，比正文行高一档。
const LIST_ITEM_HEIGHT = 36;
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
