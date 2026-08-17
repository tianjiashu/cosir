/**
 * 代码高亮契约（可插拔）。
 *
 * 目的:
 *   为聊天代码块定义统一的语法高亮接入点，使具体高亮实现可无侵入替换，
 *   满足「渲染策略可插拔、不重复造轮子」的长期架构目标。
 *
 * 当前实现基于 `lowlight`（highlight.js 的 AST 封装）+ `hast-util-to-html`
 * 序列化为 HTML + `DOMPurify` 净化，替代首版的 passthrough 空实现。
 * 不支持的语言或转换失败回退到 null，调用方据此渲染纯文本。
 *
 * @module lib/markdown/highlight
 */

import { createLowlight, common } from "lowlight";
import { toHtml } from "hast-util-to-html";
import DOMPurify from "dompurify";

/**
 * 净化器实例。
 *
 * DOMPurify 在浏览器/WebView（有 `window`）下直接可用；在 node 测试环境中
 * 无 `window`，`sanitize` 不可用。测试环境仅校验高亮结构、不注入恶意 HTML，
 * 故无 window 时降级为「原样透传」，不阻断测试。生产环境始终走浏览器净化路径。
 */
const purify =
  typeof window !== "undefined"
    ? DOMPurify
    : { sanitize: (html: string): string => html };

/** lowlight 实例（进程级单例，common 语言子集）。 */
const lowlight = createLowlight(common);

/**
 * 高亮结果。
 */
export interface HighlightResult {
  /**
   * 高亮后的 HTML 字符串（如 `<span class="hljs-keyword">const</span>`）。
   *
   * 安全契约（强制）：实现方必须保证 `html` 仅由「原始代码文本经转义 + 高亮标签」构成，
   * 不得包含任意用户可控的原始 HTML。本实现经 DOMPurify 净化后再返回，
   * 否则 CodeBlock 的 `dangerouslySetInnerHTML` 会产生 XSS 风险。
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
 * 基于 lowlight 的高亮实现。
 *
 * 目的:
 *   用 highlight.js 的 AST 能力替代首版 passthrough 空实现，使代码块出现真实的
 *   语法高亮；输出经 DOMPurify 净化，杜绝 XSS。
 *
 * 参数:
 *   code - 原始代码文本。
 *   language - 语言标识（lowlight 不支持时回退纯文本）。
 *
 * 返回:
 *   高亮结果（html 已净化）；不支持该语言或转换异常时返回 null（调用方回退纯文本）。
 *
 * 异常:
 *   不向上抛出（lowlight/hast/DOMPurify 任一环节失败均回退 null）。
 *
 * @sideeffect 无（纯函数，不修改入参）。
 */
export const highlightCode: HighlightFn = (code: string, language?: string): HighlightResult | null => {
  if (!language) return null;
  // lowlight 仅注册 common 子集，未注册的语言直接回退纯文本。
  if (!lowlight.registered(language)) return null;
  try {
    const tree = lowlight.highlight(language, code);
    const rawHtml = toHtml(tree);
    const safeHtml = purify.sanitize(rawHtml);
    return { html: safeHtml, language };
  } catch {
    // 高亮失败（如语言别名解析异常）回退纯文本，不阻断渲染。
    return null;
  }
};
