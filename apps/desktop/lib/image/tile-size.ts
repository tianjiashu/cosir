/**
 * 图片缩略图（`components/image/image-tile.tsx`）的两档尺寸，单位 px，含边框。
 *
 * `compact` 用于输入框的待发图片（密度优先）；`regular` 用于对话里的全部图片（用户消息、工具读到或
 * 产出的图片），同一种内容只有一种外观。消息行高估计（`message-height-estimate`）与多图收叠的排版
 * 直接读这里，改尺寸不必再手工同步别处的数字。
 */
export const IMAGE_TILE_SIZE = {
  compact: { width: 64, height: 64 },
  regular: { width: 120, height: 96 },
} as const;

export type ImageTileSize = keyof typeof IMAGE_TILE_SIZE;

/** 对话图片之间的间距（`gap-2`）。 */
export const IMAGE_TILE_GAP = 8;
