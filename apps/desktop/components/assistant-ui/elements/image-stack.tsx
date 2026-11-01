import { Fragment, useState, type ReactNode } from "react";

import { IMAGE_TILE_GAP, IMAGE_TILE_SIZE } from "@/lib/image/tile-size";
import { IMAGES_PER_ROW } from "@/lib/assistant/image-stack-policy";

/** 展开后最宽排几张（与行高估计的每行张数一致），胶囊因此贴着图片而不是被推到行首；窄时照常折行。 */
const EXPANDED_MAX_WIDTH = IMAGES_PER_ROW * IMAGE_TILE_SIZE.regular.width + (IMAGES_PER_ROW - 1) * IMAGE_TILE_GAP;

/**
 * 多图收成一叠：收起时只显示第一张、后面错开露出两层，旁边是「展开 N」；展开后全部铺开，胶囊变成「收起」。
 *
 * 只管排版与展开状态，每张图怎么渲染（地址、放大预览、渲染隔离）由调用方经 `renderTile` 决定。
 * 展开状态是组件内的界面状态：虚拟列表把行卸载后回到收起，与工具卡片、思考过程的折叠一致。
 * 收起时只挂载第一张，图多的消息因此只加载一张图。
 */
export function ImageStack({ count, renderTile }: { count: number; renderTile: (index: number) => ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div data-slot="image-stack" data-expanded={expanded} className="flex items-center gap-3">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
        className="bg-muted text-muted-foreground hover:text-foreground focus-ring shrink-0 cursor-pointer rounded-full px-2.5 py-1 text-xs"
      >
        {expanded ? "收起" : `展开 ${count}`}
      </button>
      {expanded ? (
        <div className="flex flex-wrap justify-end gap-2" style={{ maxWidth: EXPANDED_MAX_WIDTH }}>
          {Array.from({ length: count }, (_, index) => <Fragment key={index}>{renderTile(index)}</Fragment>)}
        </div>
      ) : (
        <div className="relative">
          <div aria-hidden="true" className="bg-muted border-border/70 absolute inset-0 translate-x-2.5 translate-y-1 rotate-6 rounded-lg border shadow-sm" />
          <div aria-hidden="true" className="bg-muted border-border/70 absolute inset-0 translate-x-1 rotate-3 rounded-lg border shadow-sm" />
          <div className="relative rounded-lg shadow-sm">{renderTile(0)}</div>
        </div>
      )}
    </div>
  );
}
