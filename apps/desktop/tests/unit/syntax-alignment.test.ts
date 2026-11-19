import { highlightTree } from "@lezer/highlight";
import { codeToTokens } from "shiki";
import { describe, expect, it } from "vitest";

import { languageFor } from "@/lib/syntax/code-language";
import { syntaxTheme } from "@/lib/syntax/shiki-theme";
import { syntaxHighlighter } from "@/lib/syntax/syntax-highlighter";

/**
 * 两套高亮（对话代码块的 Shiki、文件阅读器与 diff 的 CodeMirror）对同一段代码给出同一类颜色。两边分类体系不同、
 * 映射各写一份，这里按配色原则比对关键片段：标识符、属性、运算符用正文色，其余按类着色。潜在缺陷：一边改了映射，
 * 同一段代码在对话里和文件面板里颜色不同。
 */
type Category = "plain" | "keyword" | "string" | "comment" | "constant" | "function" | "tag" | "inserted" | "deleted";

function category(name: string | undefined): Category {
  if (!name || name.includes("foreground") || name.includes("punctuation") || name.includes("parameter")) return "plain";
  const match = /(keyword|string|comment|constant|function|tag|inserted|deleted)/.exec(name);
  if (!match) throw new Error(`没有对应类别：${name}`);
  return match[1] as Category;
}

/** 代码里要比对的片段：字符串、行注释、标识符、数字、运算符。 */
const WORDS = /"[^"]*"|\/\/.*|[A-Za-z_][\w]*|\d+|[=+<>-]+/g;

/** 把「起点 → 类别」的区间套到每个片段的第一个字符上。 */
function byWord(code: string, spans: { from: number; to: number; category: Category }[]): Map<string, Category> {
  const result = new Map<string, Category>();
  for (const word of code.matchAll(WORDS)) {
    result.set(word[0], spans.find((span) => span.from <= word.index && word.index < span.to)?.category ?? "plain");
  }
  return result;
}

async function shikiCategories(code: string, lang: string): Promise<Map<string, Category>> {
  const { tokens } = await codeToTokens(code, { lang: lang as never, theme: syntaxTheme });
  return byWord(code, tokens.flat().map((token) => ({ from: token.offset, to: token.offset + token.content.length, category: category(token.color) })));
}

async function codeMirrorCategories(code: string, file: string): Promise<Map<string, Category>> {
  const parser = (await languageFor(file)!.load()).language.parser;
  const spans: { from: number; to: number; category: Category }[] = [];
  highlightTree(parser.parse(code), syntaxHighlighter, (from, to, name) => spans.push({ from, to, category: category(name) }));
  return byWord(code, spans);
}

const SAMPLES: { file: string; lang: string; code: string; expected: Record<string, Category> }[] = [
  {
    file: "a.ts", lang: "ts", code: 'const total = count + 1; // note\nfunction run() { return "done"; }',
    expected: { const: "keyword", total: "plain", count: "plain", "=": "plain", "+": "plain", "1": "constant", "// note": "comment", run: "function", return: "keyword", '"done"': "string" },
  },
  {
    file: "a.json", lang: "json", code: '{"name": "cosir", "count": 3}',
    expected: { '"name"': "plain", '"cosir"': "string", "3": "constant" },
  },
  {
    file: "a.html", lang: "html", code: '<div class="box">hi</div>',
    expected: { div: "tag", class: "function", '"box"': "string" },
  },
];

describe("两套高亮按同一原则着色", () => {
  it.each(SAMPLES)("$file", async ({ file, lang, code, expected }) => {
    const [shiki, codeMirror] = await Promise.all([shikiCategories(code, lang), codeMirrorCategories(code, file)]);
    for (const [word, want] of Object.entries(expected)) {
      expect(codeMirror.get(word), `CodeMirror ${file}「${word}」`).toBe(want);
      expect(shiki.get(word), `Shiki ${file}「${word}」`).toBe(want);
    }
  });

  it("```diff 代码块的增删行有颜色（变量都已定义）", async () => {
    const { tokens } = await codeToTokens("+added\n-removed", { lang: "diff", theme: syntaxTheme });
    const colors = tokens.map((line) => category(line[0].color));
    expect(colors).toEqual(["inserted", "deleted"]);
  });
});
