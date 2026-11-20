import type { CodeHighlighterPlugin } from "streamdown";
import type { TokensResult } from "shiki/core";
import { bundledLanguagesInfo } from "shiki/langs";

import { HIGHLIGHT_THEMES, languageHighlighter, readyHighlighter } from "@/lib/syntax/highlighter";
import { highlightLanguageOfFence } from "@/lib/syntax/code-language";

/** 首次用到某个语言时，等它加载的代码块（同一段代码的多个回调合并成一次计算）。 */
const waiting = new Map<string, Set<(result: TokensResult) => void>>();

/**
 * Markdown 代码块（Streamdown）的着色插件：用全应用唯一的着色服务（`lib/syntax/highlighter.ts`），与文件阅读器、
 * diff 同一套语法与主题。取代 `@streamdown/code` 自带的那份（它会另建一个 Shiki 实例）。
 *
 * 契约是 Streamdown 的：语言已就绪时当场算、同步返回；否则返回 null，加载完后经回调交回。不缓存结果：Streamdown
 * 只在代码或语言变了时才调用。主题由服务决定，忽略调用方传的 `themes`。
 */
export const codeHighlightPlugin: CodeHighlighterPlugin = {
  name: "shiki",
  type: "code-highlighter",
  supportsLanguage: (language) => highlightLanguageOfFence(language) !== null,
  getSupportedLanguages: () => bundledLanguagesInfo.map((info) => info.id) as ReturnType<CodeHighlighterPlugin["getSupportedLanguages"]>,
  getThemes: () => [HIGHLIGHT_THEMES.light, HIGHLIGHT_THEMES.dark],
  highlight: ({ code, language }, callback) => {
    const lang = highlightLanguageOfFence(language);
    if (!lang) return null;
    const highlighter = readyHighlighter(lang);
    if (highlighter) return highlighter.codeTokens(code);
    if (!callback) return null;
    const key = `${lang}\u0000${code}`;
    const pending = waiting.get(key);
    if (pending) {
      pending.add(callback);
      return null;
    }
    waiting.set(key, new Set([callback]));
    void languageHighlighter(lang).then((loaded) => {
      const callbacks = waiting.get(key)!;
      waiting.delete(key);
      if (!loaded) return;
      const result = loaded.codeTokens(code);
      for (const deliver of callbacks) deliver(result);
    });
    return null;
  },
};
