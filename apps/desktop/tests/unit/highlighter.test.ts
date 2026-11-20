import { beforeAll, describe, expect, it, vi } from "vitest";

import { createHighlighterCoreSync } from "shiki/core";
import { createOnigurumaEngine } from "shiki/engine/oniguruma";
import githubDark from "shiki/themes/github-dark-default.mjs";
import githubLight from "shiki/themes/github-light-default.mjs";

import { initHighlighting, HIGHLIGHT_THEMES, languageHighlighter, markdownHighlighter, readyHighlighter } from "@/lib/syntax/highlighter";

// 着色引擎是 WASM，应用在 `src/main.tsx` 渲染前初始化，单测在这里做同一件事。
beforeAll(initHighlighting);

// 目的：Markdown 着色器同步可用且全应用一份，着色与会话里看过什么无关，字形（加粗、倾斜）随词元给出。
// 潜在缺陷：编辑器要等异步加载；别处加载过 TypeScript 后同一份 Markdown 里的代码块突然着色；标题、强调失去字形。
describe("markdown highlighter", () => {
  it("is synchronous, shared, reproducible, and carries font styles", async () => {
    const markdown = markdownHighlighter()!;
    expect(await languageHighlighter("markdown")).toBe(markdown);
    expect(readyHighlighter("markdown")).toBe(markdown);
    const source = "# Title\n*it*\n```ts\nconst a = 1;\n```";
    const before = markdown.chunkTokens(source).lines;
    await languageHighlighter("typescript");
    expect(markdown.chunkTokens(source).lines).toEqual(before);
    expect(before[0].some((token) => token.htmlStyle?.["--shiki-light-font-weight"] === "bold")).toBe(true);
    expect(before[1].some((token) => token.htmlStyle?.["--shiki-light-font-style"] === "italic")).toBe(true);
  });
});

// 目的：会注入别的语言的语法（Angular）不改变共享实例里其他语言的颜色；潜在缺陷：同一份 HTML 模板的颜色随加载顺序变化。
describe("injecting grammars", () => {
  it("leave other languages as a fresh instance colors them", async () => {
    await languageHighlighter("angular-html");
    const html = (await languageHighlighter("html-derivative"))!;
    const fresh = createHighlighterCoreSync({ themes: [githubLight, githubDark], langs: [], engine: await createOnigurumaEngine(import("shiki/wasm")) });
    fresh.loadLanguageSync((await import("shiki/langs/html-derivative.mjs")).default);
    const source = "<p>{{ a | b }}</p>\n@if (x) { <b></b> }";
    expect(html.chunkTokens(source).lines).toEqual(fresh.codeToTokens(source, { lang: "html-derivative", themes: HIGHLIGHT_THEMES, tokenizeMaxLineLength: 5_000 }).tokens);
  });
});

// 目的：词元样式只有 `color` 与 `--shiki-*` 变量，diff 直接交给 React；潜在缺陷：出现 `font-style` 这类连字符键，React 不认。
describe("token styles", () => {
  it("use only color and custom properties", async () => {
    const markdown = markdownHighlighter()!;
    const ts = (await languageHighlighter("typescript"))!;
    const keys = [...markdown.chunkTokens("# T\n*i* **b** ~~s~~").lines, ...ts.chunkTokens("const a = `x${1}`; // c").lines]
      .flat().flatMap((token) => Object.keys(token.htmlStyle ?? {}));
    expect(keys.every((key) => key === "color" || key.startsWith("--"))).toBe(true);
  });
});

// 目的：一次着色整段代码有上限，超过按原文分行；潜在缺陷：新建大文件的 diff、长代码块在渲染时同步着色卡住界面。
describe("whole-code highlighting", () => {
  it("returns plain lines past the length limit", async () => {
    const ts = (await languageHighlighter("typescript"))!;
    const code = `${"const a = 1;\n".repeat(2_000)}\nlast`;
    const { tokens } = ts.codeTokens(code);
    expect(tokens).toHaveLength(2_002);
    expect(tokens[0]).toEqual([{ content: "const a = 1;", offset: 0 }]);
    expect(tokens[2_000]).toEqual([]);
    expect(tokens[2_001]).toEqual([{ content: "last", offset: code.length - 4 }]);
  });
});

// 目的：引擎初始化失败时全应用按原文显示；潜在缺陷：Markdown 着色器同步抛错，配置编辑器与 ```md 代码块渲染坏掉。
describe("without the engine", () => {
  it("gives no markdown highlighter and the code block stays raw", async () => {
    vi.resetModules();
    const service = await import("@/lib/syntax/highlighter");
    const { codeHighlightPlugin } = await import("@/components/markdown/code-highlight-plugin");
    expect(service.markdownHighlighter()).toBeNull();
    const callback = vi.fn();
    expect(codeHighlightPlugin.highlight({ code: "# a", language: "md", themes: [HIGHLIGHT_THEMES.light, HIGHLIGHT_THEMES.dark] }, callback)).toBeNull();
    expect(await service.languageHighlighter("markdown")).toBeNull();
    expect(callback).not.toHaveBeenCalled();
  });
});
