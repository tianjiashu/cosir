import { IMAGE_TILE_SIZE } from "@/lib/image/tile-size";
import type { TransportDeliverable, TransportMessage, TransportToolCallPart } from "../contract";

/** 聚合卡里的一项：记录本身与来自哪次调用（同一 `key` 只留最后一次，`toolCallId` 也随之取最后一次）。 */
export type DeliverableEntry = { deliverable: TransportDeliverable; toolCallId: string };

/**
 * 一个回合的全部交付物合成的一张卡（`present_deliverables` 可以在一个回合里调用多次）。
 * `index` 是本回合第一次**带记录**的调用的位置：失败或取消的调用没有记录，不占位。
 */
export type DeliverablesCard = { kind: "deliverables"; index: number; items: DeliverableEntry[]; callIndexes: number[] };

/** 胶囊每行按对话内容列最大宽度下的枚数估高；窄窗口的误差由虚拟列表的实测高度修正。 */
export const CAPSULES_PER_ROW = 3;

/**
 * 按调用顺序合并交付记录：同一 `key`（后端算好的判等值）只留最后一次，位置也取最后一次——与工具内去重
 * 同一规则，模型「再交付一遍」时卡片落在最新的位置。规则只在这里，回合布局与估高都调它。
 */
export function collectDeliverables(calls: ReadonlyArray<{ index: number; part: TransportToolCallPart }>): DeliverablesCard | null {
  const recorded = calls.filter((call) => call.part.deliverables?.length);
  if (!recorded.length) return null;
  const byKey = new Map<string, DeliverableEntry>();
  for (const { part } of recorded) {
    for (const deliverable of part.deliverables ?? []) {
      byKey.delete(deliverable.key);
      byKey.set(deliverable.key, { deliverable, toolCallId: part.toolCallId });
    }
  }
  return { kind: "deliverables", index: recorded[0].index, items: [...byKey.values()], callIndexes: recorded.map((call) => call.index) };
}

/** 方块一行按对话内容列宽度下的枚数估高（120 宽、间距 8）。 */
export const TILES_PER_ROW = 5;

/**
 * 排版类别：工作区内的位图与 SVG 是方块（有画面可看），其余是胶囊。工作区外的图片也是胶囊：文件面板读不了工作区外
 * 的文件，没有图源。类别只看交付时的记录，不等现读，回合布局与估高都不必先读文件信息。
 */
export function isTile(deliverable: TransportDeliverable): boolean {
  return deliverable.type === "file" && deliverable.workspacePath !== null && (deliverable.kind === "image" || deliverable.kind === "svg");
}

/** 胶囊高（px）：胶囊本身（`deliverable-capsule`）与估高共用，改高度只改这里。 */
export const CAPSULE_HEIGHT = 36;

/** 单张大图卡的显示区上限（px）；资源用 `preview=card`（后端装进 720×480，两倍清晰度）。 */
export const IMAGE_CARD_MAX = { width: 360, height: 240 } as const;

/** 按原图比例装进 360×240，小图不放大。 */
export function cardSize(width: number, height: number): { width: number; height: number } {
  const scale = Math.min(1, IMAGE_CARD_MAX.width / width, IMAGE_CARD_MAX.height / height);
  return { width: Math.round(width * scale), height: Math.round(height * scale) };
}

/** 单张大图卡：图片与它占的尺寸。 */
export type DeliverableImageCard = { entry: DeliverableEntry; size: { width: number; height: number } };

/**
 * 方块一行在上、胶囊在下。本回合只有一张图、记录里有宽高（SVG 与读不出的位图没有），且按比例装进 360×240 后
 * 不窄于方块时，它放大成单张大图卡（`card`）；竖长截图装进去只剩一条细线，不如方块裁切保留顶部。判定只看交付记录，
 * 渲染与估高用同一个结果，首帧就是最终形态。
 */
export function deliverableGroups(card: DeliverablesCard): { tiles: DeliverableEntry[]; capsules: DeliverableEntry[]; card: DeliverableImageCard | null } {
  const tiles = card.items.filter((entry) => isTile(entry.deliverable));
  const capsules = card.items.filter((entry) => !isTile(entry.deliverable));
  const only = tiles.length === 1 ? tiles[0] : null;
  const record = only?.deliverable.type === "file" ? only.deliverable : null;
  const size = record?.width && record.height ? cardSize(record.width, record.height) : null;
  const imageCard = only && size && size.width >= IMAGE_TILE_SIZE.regular.width ? { entry: only, size } : null;
  return { tiles: imageCard ? [] : tiles, capsules, card: imageCard };
}

/** 估高用的尺寸：方块行数、单张大图卡的高度（没有时为 0）、胶囊行数；实际高度由虚拟列表实测修正。 */
export function deliverableRows(card: DeliverablesCard): { tileRows: number; cardHeight: number; capsuleRows: number } {
  const groups = deliverableGroups(card);
  return {
    tileRows: Math.ceil(groups.tiles.length / TILES_PER_ROW),
    cardHeight: groups.card?.size.height ?? 0,
    capsuleRows: Math.ceil(groups.capsules.length / CAPSULES_PER_ROW),
  };
}

/**
 * 已加载消息里工作区内交付文件的路径（排序去重）：会话订阅界面据此登记文件监听，卡片的现读信息随文件变化刷新。
 * 只认已加载的消息——没加载的历史不渲染卡片，不需要监听；目录与工作区外的条目不现读，也不登记。
 */
export function workspaceDeliverablePaths(messages: readonly TransportMessage[]): string[] {
  const paths = new Set<string>();
  for (const message of messages)
    for (const part of message.parts)
      if (part.type === "tool-call")
        for (const deliverable of part.deliverables ?? [])
          if (deliverable.type === "file" && deliverable.workspacePath !== null) paths.add(deliverable.workspacePath);
  return [...paths].sort();
}
