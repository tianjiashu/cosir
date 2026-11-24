import { IMAGE_TILE_SIZE } from "@/lib/image/tile-size";
import type { TransportDeliverable, TransportToolCallPart } from "../contract";

/** 交付卡里的一项：记录本身与来自哪次调用。 */
export type DeliverableEntry = { deliverable: TransportDeliverable; toolCallId: string };

/**
 * 一次 `present_deliverables` 调用的交付卡：放在调用的位置（`index`），顶部是这批的说明（展示数据的 `note`），
 * 下面是这批的成果。一次一张而不是整个回合合成一张：说明属于它那一批，合在一起就对不上号。
 */
export type DeliverablesBatch = { kind: "deliverables"; index: number; part: TransportToolCallPart; note: string | null; items: DeliverableEntry[] };

/** 胶囊每行按对话内容列最大宽度下的枚数估高；窄窗口的误差由虚拟列表的实测高度修正。 */
export const CAPSULES_PER_ROW = 3;

/** 这批交付的说明（后端已去空白、空串为 null）；不是交付卡的展示数据时为 null。 */
export function deliverablesNote(data: TransportToolCallPart["display_data"]): string | null {
  return data?.kind === "deliverables" && typeof data.note === "string" ? data.note : null;
}

/**
 * 把一个回合里带记录的调用排成交付卡，按调用顺序。同一 `key`（后端算好的判等值）只留最后一次——与工具内去重
 * 同一规则，模型「再交付一遍」时只出现在后一张卡里；前一张的成果全被后来重交时整张不出现（它的说明也随之作废）。
 * 失败、取消、运行中的调用没有记录，不成卡。规则只在这里，回合布局与估高都调它。
 */
export function collectDeliverableBatches(calls: ReadonlyArray<{ index: number; part: TransportToolCallPart }>): DeliverablesBatch[] {
  const latest = new Map<string, string>();
  for (const { part } of calls) for (const deliverable of part.deliverables ?? []) latest.set(deliverable.key, part.toolCallId);
  return calls.flatMap(({ index, part }) => {
    const items = (part.deliverables ?? []).filter((deliverable) => latest.get(deliverable.key) === part.toolCallId)
      .map((deliverable) => ({ deliverable, toolCallId: part.toolCallId }));
    return items.length ? [{ kind: "deliverables" as const, index, part, note: deliverablesNote(part.display_data), items }] : [];
  });
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
 * 方块一行在上、胶囊在下。这张卡只有一张图、记录里有宽高（SVG 与读不出的位图没有），且按比例装进 360×240 后
 * 不窄于方块时，它放大成单张大图卡（`card`）；竖长截图装进去只剩一条细线，不如方块裁切保留顶部。判定只看交付记录，
 * 渲染与估高用同一个结果，首帧就是最终形态。
 */
export function deliverableGroups(card: Pick<DeliverablesBatch, "items">): { tiles: DeliverableEntry[]; capsules: DeliverableEntry[]; card: DeliverableImageCard | null } {
  const tiles = card.items.filter((entry) => isTile(entry.deliverable));
  const capsules = card.items.filter((entry) => !isTile(entry.deliverable));
  const only = tiles.length === 1 ? tiles[0] : null;
  const record = only?.deliverable.type === "file" ? only.deliverable : null;
  const size = record?.width && record.height ? cardSize(record.width, record.height) : null;
  const imageCard = only && size && size.width >= IMAGE_TILE_SIZE.regular.width ? { entry: only, size } : null;
  return { tiles: imageCard ? [] : tiles, capsules, card: imageCard };
}

/** 估高用的尺寸：方块行数、单张大图卡的高度（没有时为 0）、胶囊行数；实际高度由虚拟列表实测修正。 */
export function deliverableRows(card: Pick<DeliverablesBatch, "items">): { tileRows: number; cardHeight: number; capsuleRows: number } {
  const groups = deliverableGroups(card);
  return {
    tileRows: Math.ceil(groups.tiles.length / TILES_PER_ROW),
    cardHeight: groups.card?.size.height ?? 0,
    capsuleRows: Math.ceil(groups.capsules.length / CAPSULES_PER_ROW),
  };
}
