/**
 * Markdown 配置文档在前端的最小公共契约。
 *
 * 该模块只承载配置编辑器需要的文档元数据和 Token 估算，不负责请求、持久化或页面状态。
 */

export type MarkdownDocumentContract = {
  content: string;
  path: string;
  token_length: number;
  max_tokens: number;
};

/**
 * 按后端 TokenEstimator 的字符分类规则估算文本 Token 数。
 *
 * 中文及全角字符按 1 个 Token 估算，其余字符每 4 个估算为 1 个 Token；非空文本至少返回 1。
 * 这是编辑器的即时提示，不替代后端保存前的最终校验。
 */
export function estimateMarkdownTokens(text: string): number {
  if (!text) return 0;
  const characters = [...text];
  let cjk = 0;
  for (const character of characters) {
    const code = character.codePointAt(0) ?? 0;
    if (
      (code >= 0x4e00 && code <= 0x9fff)
      || (code >= 0x3400 && code <= 0x4dbf)
      || (code >= 0x3000 && code <= 0x303f)
      || (code >= 0xff00 && code <= 0xffef)
    ) cjk += 1;
  }
  return Math.max(1, cjk + Math.floor((characters.length - cjk) / 4));
}
