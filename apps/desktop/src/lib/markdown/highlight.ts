/**
 * 代码高亮契约（可插拔）。
 *
 * 目的:
 *   为聊天代码块定义统一的语法高亮接入点，使具体高亮实现（首版不引入依赖，
 *   后续可接 `rehype-highlight` / `lowlight` / shiki 等）可无侵入替换，
 *   满足「渲染策略可插拔、不重复造轮子」的长期架构目标。
 *
 * 当前实现为 **passthrough（透传）**：返回 `null` 表示「不做高亮」，
 * 调用方据此回退到纯文本 `<code>` 渲染。这样首版零依赖、零风险，
 * 待定稿期高亮需求明确后再注入真实实现，无需改动 CodeBlock 调用点。
 *
 * @module lib/markdown/highlight
 */

/** 高亮结果。 */
export interface HighlightResult {
  /**
   * 高亮后的 HTML 字符串（如 `<span class="hljs-keyword">const</span>`）。
   *
   * 安全契约（强制）：实现方必须保证 `html` 仅由「原始代码文本经转义 + 高亮标签」构成，
   * 不得包含任意用户可控的原始 HTML。接入第三方实现（如 lowlight/shiki/rehype-highlight）
   * 时，若其输出未经可信转义，须经 DOMPurify 等净化后再返回，否则 CodeBlock 的
   * `dangerouslySetInnerHTML` 会产生 XSS 风险。首版 passthrough 恒返回 null，无此风险。
   */
  html: string;
  /** 实际使用的语言标识（可能经规范化，如 `js` → `javascript`）。 */
  language: string;
}

/**
 * 语法高亮函数契约。
 *
 * @param code - 原始代码文本。
 * @param language - 语言标识（可能为 undefined，表示未知语言）。
 * @returns 高亮结果；若不支持该语言或无需高亮则返回 null（调用方回退纯文本）。
 */
export type HighlightFn = (code: string, language?: string) => HighlightResult | null;

/**
 * 默认高亮实现：透传（不高亮）。
 *
 * 目的:
 *   首版不引入高亮依赖，保持零风险；返回 null 让 CodeBlock 回退到纯文本渲染。
 *   后续接入真实高亮实现时，只需替换 {@link highlightCode} 的实现或新增
 *   `createHighlighter` 工厂，调用点（CodeBlock）不变。
 *
 * 参数:
 *   _code - 原始代码文本（当前忽略）。
 *   _language - 语言标识（当前忽略）。
 *
 * 返回:
 *   始终返回 null（表示不做高亮）。
 *
 * 异常:
 *   不抛出。
 *
 * @sideeffect 无。
 */
export const highlightCode: HighlightFn = (_code: string, _language?: string): HighlightResult | null => {
  return null;
};
