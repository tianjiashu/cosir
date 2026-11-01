/** 一次查找的位置：`index` 从 0 起；`index < 0` 且 `count > 0` 表示命中太多、只知道至少 `count` 处。 */
export type FindResult = { index: number; count: number };
export type FindDirection = "next" | "previous";

/** 单次查找最多标出的命中数；更多时只报告「至少这么多」，避免超大文本卡住界面。 */
export const MAX_FIND_MATCHES = 5000;

/**
 * 从按位置排好序的命中起点里选出这次要停的那一处（源码与渲染视图共用的规则）。
 *
 * - 换了查找词（`fresh`）：停在 `anchor` 处或之后的第一处，边输入边查时不会跳过光标所在的命中；
 * - 同一个词再查：严格越过 `anchor`，向前或向后一处；两端回绕。
 */
export function nextMatchIndex(starts: readonly number[], anchor: number, direction: FindDirection, fresh: boolean): number {
  if (direction === "previous") {
    for (let index = starts.length - 1; index >= 0; index--) if (starts[index] < anchor) return index;
    return starts.length - 1;
  }
  const found = starts.findIndex((start) => (fresh ? start >= anchor : start > anchor));
  return found < 0 ? 0 : found;
}
