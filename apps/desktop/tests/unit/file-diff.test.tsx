import { renderToStaticMarkup } from "react-dom/server";
import { markEdits, pickRanges, tokenize } from "react-diff-view";
import { parseDiff } from "react-diff-view";
import { describe, expect, it } from "vitest";

import { FileDiff, maxLineNumber } from "@/components/conversation/tools/file-diff";
import { readFileChanges } from "@/lib/assistant/tools/file-changes";
import { languageFor } from "@/lib/syntax/code-language";
import { diffSyntaxRanges, diffTokens } from "@/lib/syntax/diff-tokens";

const PATCH = `diff --git a/a.ts b/a.ts
--- a/a.ts
+++ b/a.ts
@@ -10,3 +10,4 @@ class Store {
 const a = 1;
-let b = "x";
+/* one
+two */ let b = "y";
 done();
@@ -80,2 +81,2 @@ class Store {
-  remove() {}
+  remove(id) {}
 }
`;

const hunks = () => parseDiff(PATCH)[0].hunks;

// 目的：高亮区间用文件里的真实行号与本行列号，跨行语法元素逐行切开；潜在缺陷：用段内序号当行号、跨行区间画到下一行开头之外。
describe("diff syntax ranges", () => {
  it("maps highlight spans to real line numbers on each side", async () => {
    const support = await languageFor("a.ts")!.load();
    const [oldRanges, newRanges] = diffSyntaxRanges(hunks(), support.language.parser);
    const at = (ranges: typeof oldRanges, lineNumber: number, className: string) =>
      ranges.filter((range) => range.lineNumber === lineNumber && range.properties.className === className).map(({ start, length }) => [start, length]);

    expect(at(oldRanges, 10, "syntax-keyword")).toEqual([[0, 5]]);
    expect(at(oldRanges, 11, "syntax-string")).toEqual([[8, 3]]);
    expect(at(newRanges, 11, "syntax-comment")).toEqual([[0, 6]]);
    expect(at(newRanges, 12, "syntax-comment")).toEqual([[0, 6]]);
    expect(at(newRanges, 12, "syntax-keyword")).toEqual([[7, 3]]);
    expect(at(newRanges, 13, "syntax-function")).toEqual([[0, 4]]);
    expect(newRanges.every((range) => range.length > 0)).toBe(true);
  });
});

// 目的：两段改动之间看得出跳过了多少行、在哪段代码里；潜在缺陷：两段贴在一起读成连续代码。
describe("file diff hunk gap", () => {
  it("separates hunks with the skipped line count and context", () => {
    const change = readFileChanges([{ path: "a.ts", status: "modified", patch: PATCH, insertions: 3, deletions: 2 }])[0];
    const html = renderToStaticMarkup(<FileDiff change={change} />);
    expect(html.match(/跳过/g)).toHaveLength(1);
    expect(html).toContain("⋯ 跳过 67 行");
    expect(html).toContain("class Store {");
  });
});

// 目的：语言只按文件名认；潜在缺陷：diff 传的是带目录的路径，Dockerfile 这类按名字认的文件不高亮。
describe("language by file", () => {
  it("recognizes name-matched files under directories", () => {
    expect(languageFor("docker/Dockerfile")?.name).toBe("Dockerfile");
    expect(languageFor("Dockerfile")?.name).toBe("Dockerfile");
    expect(languageFor("src/app/main.ts")?.name).toBe("TypeScript");
  });
});

// 目的：diff 高亮的计算量跟着改动走、不跟着行号走，且结果与整份计算一致；潜在缺陷：改动在大文件末尾时展开卡顿。
describe("diff tokens", () => {
  it("matches whole-file tokenize, line by line", async () => {
    const parser = (await languageFor("a.ts")!.load()).language.parser;
    const whole = tokenize(hunks(), { enhancers: [pickRanges(...diffSyntaxRanges(hunks(), parser)), markEdits(hunks(), { type: "block" })] });
    const byHunk = diffTokens(hunks(), parser);
    // 区间节点带着计算时的行号（按段算时是平移后的），渲染不用它，比对时去掉。
    const rendered = (value: unknown) => JSON.parse(JSON.stringify(value, (key, field) => (key === "lineNumber" ? undefined : field)));
    for (const side of ["old", "new"] as const) {
      whole[side].forEach((line, index) => {
        if (line.length && !(line.length === 1 && line[0].type === "text" && !line[0].value)) expect(rendered(byHunk[side][index]), `${side} ${index + 1}`).toEqual(rendered(line));
      });
    }
  });

  it("stays cheap for a change deep in a large file", () => {
    const deep = parseDiff(`diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -200000,3 +200000,3 @@\n a\n-foo(a);\n+bar(b);\n b\n`)[0].hunks;
    const started = performance.now();
    const tokens = diffTokens(deep, null);
    expect(performance.now() - started).toBeLessThan(50);
    expect(JSON.stringify(tokens.new[200000])).toContain("edit");
  });

  it("sizes the gutter by the last line number", () => {
    const ending99 = parseDiff(`diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -97,3 +97,3 @@\n a\n-x\n+y\n b\n`)[0].hunks;
    expect(maxLineNumber(ending99)).toBe(99);
  });
});
