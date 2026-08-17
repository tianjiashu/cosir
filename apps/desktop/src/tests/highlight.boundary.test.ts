/**
 * 代码高亮（§1）**边界补充测试**。
 *
 * 现有 `highlight.test.ts` 只覆盖了 happy path + 两处 null 回退。
 * 本文件补：空字符串/空白语言标识、语言别名、hljs 类名结构、HTML 转义、
 * 恶意输入的原始标签必须被转义而非原样输出、纯函数无副作用、返回 language 契约。
 *
 * 关于 DOMPurify：node（默认 vitest environment）下无 `window`，源码降级为
 * 「原样透传」，因此**在 node 环境无法验证净化行为**。本文件的 XSS 相关断言
 * 依赖的是 lowlight/hast-util-to-html 自身的 HTML 转义（这是第一道且与环境无关的
 * 防线）；DOMPurify 净化路径另在 `highlight.dompurify.test.ts`（happy-dom 环境）验证。
 */

import { describe, it, expect } from "vitest";
import { highlightCode } from "@/lib/markdown/highlight";

describe("highlightCode 语言标识边界（§1）", () => {
  // 测试目的：空字符串语言标识应走 falsy 分支回退 null，而非交给 lowlight 报错。
  // 可能发现的缺陷：用 `language === undefined` 判空，空串穿透到 lowlight 抛异常。
  it("空字符串语言回退 null", () => {
    expect(highlightCode("code", "")).toBeNull();
  });

  // 测试目的：仅空白的语言标识不是已注册语言，应回退 null 且不抛。
  // 可能发现的缺陷：未做 registered 校验导致 lowlight 抛「Unknown language」。
  it("空白语言标识回退 null 且不抛异常", () => {
    expect(() => highlightCode("code", "   ")).not.toThrow();
    expect(highlightCode("code", "   ")).toBeNull();
  });

  // 测试目的：常见别名（js/ts/py）在 lowlight common 子集中应被识别为已注册。
  // 可能发现的缺陷：只认全名，导致 markdown 里最常用的 ```js 代码块完全不高亮。
  it("常见别名 js/ts/py 可高亮", () => {
    for (const lang of ["js", "ts", "py"]) {
      const out = highlightCode("const x = 1", lang);
      expect(out, `别名 ${lang} 未被识别`).not.toBeNull();
      expect(out?.html).toContain("hljs");
    }
  });

  // 测试目的：返回的 language 必须是调用方传入的原标识（契约：调用方据此渲染 class）。
  // 可能发现的缺陷：返回规范化后的名字导致调用方 class 名与预期不一致。
  it("返回的 language 与传入标识一致", () => {
    expect(highlightCode("x=1", "js")?.language).toBe("js");
    expect(highlightCode("x=1", "javascript")?.language).toBe("javascript");
  });

  // 测试目的：空代码 + 合法语言不应抛异常，应返回空/短 HTML（非 null 亦可接受）。
  // 可能发现的缺陷：空输入导致 hast 树为空时序列化崩溃。
  it("空代码文本不抛异常", () => {
    expect(() => highlightCode("", "javascript")).not.toThrow();
    const out = highlightCode("", "javascript");
    expect(out).not.toBeNull();
    expect(typeof out?.html).toBe("string");
  });
});

describe("highlightCode 输出结构与转义（§1 安全第一道防线）", () => {
  // 测试目的：高亮产物应包含 hljs-* 语义类名（而非仅裸文本），确认真实走了高亮管线。
  // 可能发现的缺陷：退回 passthrough 空实现，html 无任何 hljs class，UI 无高亮。
  it("产物包含 hljs-* 语义类名", () => {
    const out = highlightCode("const x = 1;", "javascript");
    expect(out?.html).toMatch(/class="hljs-[a-z]+"/);
  });

  // 测试目的：代码中的 `<` `>` `&` 必须被转义为实体，不得产生真实 DOM 标签。
  // 可能发现的缺陷：未转义导致 dangerouslySetInnerHTML 注入任意 HTML（XSS）。
  it("代码中的尖括号与 & 被转义为实体", () => {
    const out = highlightCode("const a = b < c && d > e;", "javascript");
    expect(out?.html).toContain("&#x3C;");
    expect(out?.html).toContain("&#x26;");
    // 不得出现未转义的原始比较符构成的标签起始
    expect(out?.html).not.toContain("< c");
  });

  // 测试目的：恶意 <script> 输入不得以可执行标签形式出现在产物中。
  // 可能发现的缺陷：原始 script 标签透传 → dangerouslySetInnerHTML 触发 XSS。
  it("恶意 script 输入不产生原始 script 标签", () => {
    const payload = '<script>alert("xss")</script>';
    const out = highlightCode(payload, "javascript");
    expect(out).not.toBeNull();
    expect(out?.html).not.toContain("<script>");
    expect(out?.html).not.toContain("</script>");
    expect(out?.html).toContain("&#x3C;script");
  });

  // 测试目的：img onerror 型 payload 同样不得产生原始标签与可执行属性。
  // 可能发现的缺陷：仅过滤 script 关键字而未做通用转义。
  it("img onerror 型 payload 不产生原始标签", () => {
    const out = highlightCode('<img src=x onerror="alert(1)">', "html");
    expect(out).not.toBeNull();
    expect(out?.html).not.toContain("<img");
  });

  // 测试目的：确认为纯函数 —— 相同输入产出稳定，且不修改入参。
  // 可能发现的缺陷：lowlight 单例内部状态污染导致多次调用结果不一致（渲染抖动）。
  it("同一输入多次调用结果稳定（纯函数）", () => {
    const code = "const x = 1;";
    const a = highlightCode(code, "javascript");
    const b = highlightCode(code, "javascript");
    expect(a?.html).toBe(b?.html);
    expect(code).toBe("const x = 1;");
  });

  // 测试目的：多语言交替调用不应互相污染（单例 lowlight 的复用安全）。
  // 可能发现的缺陷：单例复用导致后续语言的高亮结果错乱。
  it("多语言交替调用不互相污染", () => {
    const js1 = highlightCode("const x = 1;", "javascript")?.html;
    highlightCode("def f(): pass", "python");
    const js2 = highlightCode("const x = 1;", "javascript")?.html;
    expect(js2).toBe(js1);
  });
});
