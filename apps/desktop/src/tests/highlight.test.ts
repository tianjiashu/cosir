/**
 * 代码高亮实现测试（§1 语法高亮）。
 *
 * 验证 lowlight + hast-util-to-html 管线：支持的语言返回带 hljs 类名的 HTML，
 * 不支持的语言 / 无语言标识回退 null（调用方渲染纯文本）。
 */

import { describe, it, expect } from "vitest";
import { highlightCode } from "@/lib/markdown/highlight";

describe("highlightCode (§1 语法高亮)", () => {
  it("对支持的语言返回带 hljs 类名的 HTML", () => {
    const out = highlightCode('const x = 1;', "javascript");
    expect(out).not.toBeNull();
    expect(out?.html).toContain("hljs");
    expect(out?.language).toBe("javascript");
  });

  it("对 python 等 common 子集语言可高亮", () => {
    const out = highlightCode('def f():\n    return 1', "python");
    expect(out).not.toBeNull();
    expect(out?.html.length).toBeGreaterThan(0);
  });

  it("无语言标识时回退 null", () => {
    expect(highlightCode("plain text")).toBeNull();
  });

  it("不支持的语言回退 null", () => {
    expect(highlightCode("some code", "unknown-lang-xyz")).toBeNull();
  });

  it("高亮产物为字符串且非空", () => {
    const out = highlightCode('console.log("hi")', "javascript");
    expect(typeof out?.html).toBe("string");
    expect(out?.html.length).toBeGreaterThan(0);
  });
});
