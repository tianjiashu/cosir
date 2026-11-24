import { parseCapsules, serializeCapsule, type Capsule, type QuoteCapsule } from "@/lib/assistant/capsules";

/** 编辑器里能出现的胶囊：摘录总在卡片区，从不进编辑器文档。 */
export type InlineCapsule = Exclude<Capsule, QuoteCapsule>;

/**
 * 输入框正文 ↔「摘录卡片 + 编辑器正文」的唯一拆合点。
 *
 * 卡片不是另一份状态：正文（草稿、撤回还原、发送都只认它）里的摘录胶囊就是卡片，输入框只把它们画在编辑器上方。
 * 合并时卡片在前、各占一行，再接编辑器正文；拆分时正文里任何位置的摘录都按出现顺序归入卡片。写正文的入口
 * （新加摘录、粘贴、失败恢复的合并）都经这里合成，不把标签直接拼在正文后面：正文以代码结尾时标签会落进代码。
 */
export function splitQuoteCards(text: string): { quotes: QuoteCapsule[]; rest: string } {
  const quotes: QuoteCapsule[] = [];
  let rest = "";
  for (const segment of parseCapsules(text)) {
    if (segment.type === "capsule" && segment.capsule.kind === "quote") quotes.push(segment.capsule);
    else rest += segment.type === "text" ? segment.text : serializeCapsule(segment.capsule);
  }
  // 合并时卡片后面的换行属于卡片区的分隔，不属于正文。
  return { quotes, rest: quotes.length ? rest.replace(/^\n+/, "") : rest };
}

export function joinQuoteCards(quotes: readonly QuoteCapsule[], rest: string): string {
  if (!quotes.length) return rest;
  const cards = quotes.map(serializeCapsule).join("\n");
  return rest ? `${cards}\n${rest}` : cards;
}
