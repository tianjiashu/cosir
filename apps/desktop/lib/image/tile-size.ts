/**
 * 历史图片缩略图（`components/image/image-tile.tsx`）的两档尺寸，单位 px，含边框。
 *
 * `regular` 用于对话里的全部图片（用户消息、工具读到或
 * 产出的图片），同一种内容只有一种外观；`inline` 只用于活动段汇总行，放进 32px 行高、不改变行高估计。消息行高估计（`message-height-estimate`）与多图收叠的排版
 * 直接读这里，改尺寸不必再手工同步别处的数字。
 */
export const IMAGE_TILE_SIZE = {
  regular: { width: 120, height: 96 },
  inline: { width: 40, height: 28 },
} as const;

export type ImageTileSize = keyof typeof IMAGE_TILE_SIZE;

/** 对话图片之间的间距（`gap-2`）。 */
export const IMAGE_TILE_GAP = 8;
