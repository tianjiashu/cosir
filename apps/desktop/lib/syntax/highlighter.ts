import { createHighlighterCoreSync, type GrammarState, type HighlighterCore, type LanguageRegistration, type RegexEngine, type ThemedToken, type TokensResult } from "shiki/core";
import { createOnigurumaEngine } from "shiki/engine/oniguruma";
import { bundledLanguages, type BundledLanguage } from "shiki/langs";
import markdownGrammar from "shiki/langs/markdown.mjs";
import githubDark from "shiki/themes/github-dark-default.mjs";
import githubLight from "shiki/themes/github-light-default.mjs";

import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 全应用唯一的代码着色服务：对话与文件预览里的 Markdown 代码块、文件阅读器与配置编辑器（CodeMirror）、工具卡片的
 * diff，颜色都只从这里来。语法与配色全部是 Shiki 官方的（TextMate 语法 + GitHub 现行的 `*-default` 主题；旧版
 * `github-dark` 的注释在深色背景上对比度不到 4.5），本仓不维护「哪类语法用什么颜色」的映射。
 *
 * 每个词元同时带浅色与深色（颜色是 `color` 与 `--shiki-dark`，字形只以 `--shiki-*-font-*`、`--shiki-*-text-decoration` 变量给出），切换只靠
 * `globals.css` 的 `.shiki-token`，不重算。只认 Shiki 的语言名，文件是什么语言由 `code-language.ts` 判断。
 */

export const HIGHLIGHT_THEMES = { light: "github-light-default", dark: "github-dark-default" } as const;

/** 单行超过这么长时整行按原文显示（Shiki 不切分这一行）：压缩过的 JS、单行 JSON 不值得为颜色卡住界面。 */
const MAX_LINE_LENGTH = 5_000;
/**
 * 一次着色整段代码（`codeTokens`）的上限，超过就整段按原文：diff 与 Markdown 代码块在渲染时同步着色，TS 约 4.4ms/千字，
 * 这里约 90ms。编辑器逐行着色、有时间预算，不受它限制。
 */
const MAX_CODE_LENGTH = 20_000;

/** 一个语言就绪后的同步着色器。 */
export type LanguageHighlighter = {
  /** 着色一段完整代码，返回 Streamdown 代码块要的结果（含整体前景、背景色）；超过上限或着色失败时按原文分行。 */
  codeTokens: (code: string) => TokensResult;
  /**
   * 着色连续文本中的一段：`state` 是上一段末尾的语法状态（从头开始时不给），返回这一段的词元与它末尾的状态。
   * 分段的结果与整段一次着色逐字一致，编辑器因此能逐行分批算（不受 `codeTokens` 的上限限制）。
   */
  chunkTokens: (code: string, state?: GrammarState) => { lines: ThemedToken[][]; state: GrammarState | undefined };
};

let engine: RegexEngine | null = null;

/**
 * 应用启动时调用一次（`src/main.tsx`；单测在各自的 `beforeAll` 里），之后建实例与着色全部同步。失败时引擎为空，
 * 全应用按原文显示：Markdown 着色器取到 null，其余语言加载失败（记日志）。正则引擎用 Oniguruma（WASM，Shiki
 * 的参考实现）而不是 JS 引擎：JS 引擎处理 TS 长行时耗时陡增（4000 字一行约 0.3 秒，Oniguruma 约 13ms），会卡住
 * 界面；WASM 只能异步实例化，所以要在启动时等它一次。
 */
export async function initHighlighting(): Promise<void> {
  engine ??= await createOnigurumaEngine(import("shiki/wasm"));
}

/** 主题随包内置，建实例是同步的；语法按需加载（Markdown 除外）。 */
function createCore(): HighlighterCore {
  if (!engine) throw new Error("着色引擎还没初始化：应用启动时须先 await initHighlighting()");
  return createHighlighterCoreSync({ themes: [githubLight, githubDark], langs: [], engine });
}

function highlighterOf(core: HighlighterCore, lang: string): LanguageHighlighter {
  const options = { lang, themes: HIGHLIGHT_THEMES, tokenizeMaxLineLength: MAX_LINE_LENGTH };
  return {
    codeTokens: (code) => {
      if (code.length > MAX_CODE_LENGTH) return plainTokens(code);
      try {
        return core.codeToTokens(code, options);
      } catch (error) {
        void frontendLog("WARNING", "code_highlight_failed", "代码着色失败，按原文显示", { data: { lang }, error });
        return plainTokens(code);
      }
    },
    chunkTokens: (code, state) => {
      const { tokens, grammarState } = core.codeToTokens(code, { ...options, grammarState: state });
      return { lines: tokens, state: grammarState };
    },
  };
}

/** 不着色的结果（每行一个原文词元，空行没有词元，与 Shiki 一致）：交出去而不是 null，Streamdown 才不会沿用上一版代码的结果。 */
function plainTokens(code: string): TokensResult {
  let offset = 0;
  const tokens = code.split("\n").map((content) => {
    const line = content ? [{ content, offset }] : [];
    offset += content.length + 1;
    return line;
  });
  return { tokens };
}

let shared: HighlighterCore | null = null;
const languages = new Map<string, Promise<LanguageHighlighter | null>>();
/** 已就绪的着色器，同步可取（Streamdown 代码块重新挂载时当场着色，不再等一轮异步）。 */
const ready = new Map<string, LanguageHighlighter>();

/**
 * Markdown 的着色器，同步可用：它是应用自己编辑的语言（配置编辑器），语法随包内置，编辑器挂载时就能装上着色、
 * 不必等加载，也就不会在用户开始输入后再为着色派发事务（原因见 `editor-highlighting.ts`）。引擎初始化失败时 null。
 */
export function markdownHighlighter(): LanguageHighlighter | null {
  if (!engine) return null;
  if (!ready.has("markdown")) ready.set("markdown", highlighterOf(isolatedCore(markdownGrammar), "markdown"));
  return ready.get("markdown")!;
}

/**
 * 着色结果会随同一实例里加载过什么而变的语法，各自独占一个实例，结果可复现：Markdown、Vue 等的部分嵌入语言是
 * 懒加载的（别处加载过就着色、没加载过就是原文）；Angular 等带 `injectTo` 的语法会注入别的语言、改变它们的颜色。
 */
function isolatedCore(grammar: LanguageRegistration[]): HighlighterCore {
  const core = createCore();
  core.loadLanguageSync(grammar);
  return core;
}

function isBundledLanguage(lang: string): lang is BundledLanguage {
  return lang in bundledLanguages;
}

/**
 * 取某个 Shiki 语言的着色器：首次用到时加载语法（异步），之后同一个 Promise 复用。语言名不是 Shiki 的时得到 null；
 * 语法加载失败时也是 null（记日志）。调用方按原文显示。
 */
export function languageHighlighter(lang: string): Promise<LanguageHighlighter | null> {
  if (lang === "markdown") return Promise.resolve(markdownHighlighter());
  const known = languages.get(lang);
  if (known) return known;
  const loading = (async () => {
    if (!isBundledLanguage(lang)) return null;
    const grammar = (await bundledLanguages[lang]()).default;
    if (grammar.some((registration) => registration.embeddedLangsLazy?.length || registration.injectTo?.length)) return highlighterOf(isolatedCore(grammar), lang);
    shared ??= createCore();
    shared.loadLanguageSync(grammar);
    return highlighterOf(shared, lang);
  })().then((highlighter) => {
    if (highlighter) ready.set(lang, highlighter);
    return highlighter;
  }).catch((error: unknown) => {
    void frontendLog("WARNING", "highlight_language_load_failed", "代码着色的语法加载失败，按原文显示", { data: { lang }, error });
    return null;
  });
  languages.set(lang, loading);
  return loading;
}

/** 已就绪的着色器（加载过且成功）；还没加载或不是 Shiki 语言时 undefined。 */
export function readyHighlighter(lang: string): LanguageHighlighter | undefined {
  return ready.get(lang);
}
