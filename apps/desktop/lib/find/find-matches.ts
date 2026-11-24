/**
 * 一段文本里查找词的全部命中（不区分大小写、按字面、不重叠），位置是原文里的 UTF-16 下标，可直接交给 `Range`。
 *
 * 不能先 `toLowerCase()` 再按下标回到原文：小写后长度会变的字符（如 `İ` → `i̇`）让之后的下标整体错开，
 * 轻则高亮偏移、重则越过文本节点长度让 `Range.setEnd` 抛错。这里用 `iu` 正则在原文上匹配，下标天然对齐；
 * 代价是只做简单大小写折叠，`i` 不匹配 `İ`。
 */
export function findMatches(text: string, needle: string, limit: number): { start: number; end: number }[] {
  const pattern = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu");
  const matches: { start: number; end: number }[] = [];
  for (let match = pattern.exec(text); match && matches.length < limit; match = pattern.exec(text)) {
    matches.push({ start: match.index, end: match.index + match[0].length });
  }
  return matches;
}
