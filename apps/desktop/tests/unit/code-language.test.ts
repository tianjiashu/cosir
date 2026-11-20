import { describe, expect, it } from "vitest";

import { highlightLanguageOf, languageFor } from "@/lib/syntax/code-language";

// 目的：语言只按文件名认；潜在缺陷：diff 传的是带目录的路径，Dockerfile 这类按名字认的文件不高亮。
describe("language by file", () => {
  it("recognizes name-matched files under directories", () => {
    expect(languageFor("docker/Dockerfile")?.name).toBe("Dockerfile");
    expect(languageFor("Dockerfile")?.name).toBe("Dockerfile");
    expect(languageFor("src/app/main.ts")?.name).toBe("TypeScript");
  });
});

// 目的：着色用的 Shiki 名只从 `languageFor` 的结果换算，方言按显式表区分。
// 潜在缺陷：按后缀另起识别把 .v（Verilog，language-data 认作 SystemVerilog）认成 V 语言、.tex 认成纯 TeX；
// .f90 用了固定格式 Fortran；Cassandra 的 .cql 被 Shiki 的别名认成 Cypher。
describe("highlight language by file", () => {
  it("follows languageFor and only splits listed dialects", () => {
    expect(highlightLanguageOf("rtl/top.v")).toBe("system-verilog");
    expect(highlightLanguageOf("paper.tex")).toBe("latex");
    expect(highlightLanguageOf("src/solver.f90")).toBe("fortran-free-form");
    expect(highlightLanguageOf("legacy.f")).toBe("fortran-fixed-form");
    expect(highlightLanguageOf("Dockerfile")).toBe("docker");
    expect(highlightLanguageOf("schema.cql")).toBe("sql");
    expect(highlightLanguageOf("notes.ipynb")).toBe("json");
    expect(highlightLanguageOf("data.unknownext")).toBeNull();
  });
});
