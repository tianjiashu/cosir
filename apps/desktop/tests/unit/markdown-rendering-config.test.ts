import { beforeAll, describe, expect, it } from "vitest";

import { cjk } from "@streamdown/cjk";
import { math } from "@streamdown/math";
import { mermaid } from "@streamdown/mermaid";

import { codeHighlightPlugin } from "@/components/markdown/code-highlight-plugin";
import { markdownPlugins } from "@/components/markdown/markdown-rendering-config";
import { initHighlighting } from "@/lib/syntax/highlighter";

// 着色引擎是 WASM，应用在 `src/main.tsx` 渲染前初始化，单测在这里做同一件事。
beforeAll(initHighlighting);

describe("markdown rendering plugins", () => {
  it("keeps the four supported Streamdown plugins in one stable configuration", () => {
    expect(markdownPlugins).toEqual({ code: codeHighlightPlugin, math, mermaid, cjk });
  });

  // 目的：代码块与文件阅读器、diff 同一个着色服务；潜在缺陷：代码块另有一份 Shiki 实例与主题，颜色对不上。
  it("highlights code blocks with the shared GitHub themes, light and dark in one pass", async () => {
    const result = await new Promise((resolve) => {
      const sync = codeHighlightPlugin.highlight({ code: "const a = 1;", language: "ts", themes: ["github-light-default", "github-dark-default"] }, resolve);
      if (sync) resolve(sync);
    });
    const first = (result as { tokens: { content: string; htmlStyle?: Record<string, string> }[][] }).tokens[0][0];
    expect(first.content).toBe("const");
    expect(first.htmlStyle?.color).toBe("#CF222E");
    expect(first.htmlStyle?.["--shiki-dark"]).toBe("#FF7B72");
    expect(codeHighlightPlugin.supportsLanguage("shell" as never)).toBe(true);
    expect(codeHighlightPlugin.supportsLanguage("no-such-language" as never)).toBe(false);
  });

  // 目的：语言已就绪时代码块当场着色；潜在缺陷：重新挂载（滚回更早的消息）时还要等一轮异步，先闪原文。
  it("returns synchronously once the language is loaded, and nothing for unknown languages", async () => {
    await new Promise((resolve) => codeHighlightPlugin.highlight({ code: "x", language: "python", themes: ["github-light-default", "github-dark-default"] }, resolve));
    const sync = codeHighlightPlugin.highlight({ code: "def f(): pass", language: "py", themes: ["github-light-default", "github-dark-default"] });
    expect(sync?.tokens[0][0].content).toBe("def");
    expect(codeHighlightPlugin.highlight({ code: "x", language: "no-such-language", themes: ["github-light-default", "github-dark-default"] }, () => undefined)).toBeNull();
  });
});
