/**
 * 长正文拆行：快照的两种投影（大纲 `outline.ts`、正文 `page-text.ts`）都不截断正文，超长的一段按句读拆成相邻几行，
 * 篇幅交给视图的分页预算（`views.ts`），`find` 也能按行命中。
 */

/** 正文一行最多多少字；更长的按句读拆成相邻几行。 */
export const TEXT_CHUNK_CHARS = 400;
/** 拆行优先断在句末，其次逗号、空格；都找不到（或太靠前）才硬切。 */
const CHUNK_BREAKS = [["。", "！", "？", "；", ". ", "! ", "? ", "; "], ["，", "、", ", "], [" "]];

/** 正文按 `TEXT_CHUNK_CHARS` 拆成几块，尽量断在句读处；不足一块的原样一块。 */
export function chunks(text: string): string[] {
  const result: string[] = [];
  let rest = text;
  while (rest.length > TEXT_CHUNK_CHARS) {
    const window = rest.slice(0, TEXT_CHUNK_CHARS);
    let at = TEXT_CHUNK_CHARS;
    for (const breaks of CHUNK_BREAKS) {
      const end = Math.max(...breaks.map((mark) => {
        const index = window.lastIndexOf(mark);
        return index < 0 ? -1 : index + mark.length;
      }));
      if (end >= TEXT_CHUNK_CHARS / 2) { at = end; break; }
    }
    result.push(rest.slice(0, at).trim());
    rest = rest.slice(at).trimStart();
  }
  if (rest) result.push(rest);
  return result;
}
