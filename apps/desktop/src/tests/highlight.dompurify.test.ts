// @vitest-environment happy-dom
/**
 * 代码高亮（§1）**生产净化路径**测试。
 *
 * 源码在模块加载时按 `typeof window !== "undefined"` 选择净化器：
 * - node 环境：降级为原样透传（仅测试环境，见 highlight.boundary.test.ts 说明）；
 * - 浏览器/WebView：走真实 DOMPurify.sanitize。
 *
 * 本文件在 happy-dom（有 window）环境下加载模块，从而覆盖**生产实际使用的
 * DOMPurify 净化分支**，确认该分支可用、不抛异常、且不破坏高亮结构。
 * 这是「node 下降级透传是预期」这一结论成立的前提验证。
 */

import { describe, it, expect } from "vitest";
import DOMPurify from "dompurify";
import { highlightCode } from "@/lib/markdown/highlight";

describe("highlightCode 在浏览器环境走 DOMPurify 净化（§1）", () => {
  // 测试目的：确认 happy-dom 下 window 存在，即模块选择的是真实 DOMPurify 分支。
  // 可能发现的缺陷：若此断言失败，则本文件其余断言并未覆盖生产路径（测试假通过）。
  it("测试环境具备 window 且 DOMPurify.sanitize 可用（前提校验）", () => {
    expect(typeof window).not.toBe("undefined");
    expect(typeof DOMPurify.sanitize).toBe("function");
  });

  // 测试目的：净化分支下高亮仍正常产出 hljs 结构，净化不会误删高亮 span/class。
  // 可能发现的缺陷：DOMPurify 默认配置剥离 class 属性，导致生产环境无高亮
  // （而 node 测试因透传永远发现不了 —— 正是此测试存在的意义）。
  it("净化后仍保留 hljs 类名与 span 结构", () => {
    const out = highlightCode("const x = 1;", "javascript");
    expect(out).not.toBeNull();
    expect(out?.html).toContain("hljs");
    expect(out?.html).toMatch(/<span class="hljs-[a-z]+"/);
  });

  // 测试目的：净化分支对恶意 payload 不抛异常且不产生可执行标签。
  // 可能发现的缺陷：净化环节异常被 catch 成 null（代码块直接不高亮），或标签透传。
  it("净化分支下恶意 payload 不产生可执行标签", () => {
    const out = highlightCode('<script>alert(1)</script>', "javascript");
    expect(out).not.toBeNull();
    expect(out?.html).not.toContain("<script>");
    expect(out?.html.toLowerCase()).not.toContain("onerror=");
  });

  // 测试目的：净化分支的转义实体在往返（sanitize）后不会被还原成活跃 HTML。
  // 可能发现的缺陷：sanitize 对已转义实体做解码，产生二次注入面。
  it("已转义实体经净化后不被还原为活跃标签", () => {
    const out = highlightCode('const s = "<img src=x onerror=alert(1)>";', "javascript");
    expect(out?.html).not.toContain("<img");
  });

  // 测试目的：净化分支下回退契约不变（未注册语言仍返回 null）。
  // 可能发现的缺陷：环境差异导致回退分支行为不一致。
  it("净化分支下未注册语言仍回退 null", () => {
    expect(highlightCode("code", "unknown-lang-xyz")).toBeNull();
    expect(highlightCode("code")).toBeNull();
  });
});
