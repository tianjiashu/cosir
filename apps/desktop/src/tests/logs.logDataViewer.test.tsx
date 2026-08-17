// @vitest-environment happy-dom
/**
 * LogDataViewer 单元测试。
 *
 * 覆盖：键值渲染、按类型着色、嵌套对象/数组折叠标记、空对象占位、
 * 以及 null/特殊数值/非法输入等边界。
 *
 * @module tests/logs.logDataViewer
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { LogDataViewer } from "@/components/logs/LogDataViewer";

afterEach(() => {
  cleanup();
});

describe("LogDataViewer 基础渲染", () => {
  // 测试目的：所有键名均渲染且带 key 高亮色。可能发现的缺陷：Object.entries 遍历遗漏或键名未加冒号。
  it("渲染所有键名（带冒号）并使用 violet 高亮", () => {
    const { container } = render(
      <LogDataViewer data={{ name: "alice", age: 30, ok: true }} />,
    );
    expect(screen.getByText("name:")).toBeTruthy();
    expect(screen.getByText("age:")).toBeTruthy();
    expect(screen.getByText("ok:")).toBeTruthy();
    expect(container.querySelectorAll(".text-violet-600").length).toBe(3);
  });

  // 测试目的：字符串值带引号且用 emerald 着色。可能发现的缺陷：引号缺失导致无法区分字符串与标识符。
  it("字符串值渲染为带引号形式并着 emerald 色", () => {
    const { container } = render(<LogDataViewer data={{ name: "alice" }} />);
    expect(container.textContent).toContain('"alice"');
    expect(container.querySelector(".text-emerald-600")).not.toBeNull();
  });

  // 测试目的：数字值用 amber 着色且不带引号。可能发现的缺陷：数字被 String 化后误判为字符串加引号。
  it("数字值不带引号并着 amber 色", () => {
    const { container } = render(<LogDataViewer data={{ count: 42 }} />);
    expect(container.textContent).toContain("42");
    expect(container.textContent).not.toContain('"42"');
    expect(container.querySelector(".text-amber-600")).not.toBeNull();
  });

  // 测试目的：布尔值渲染 true/false 并着 sky 色。可能发现的缺陷：false 被真值判断吞掉不渲染。
  it("布尔 true/false 均渲染并着 sky 色", () => {
    const { container } = render(<LogDataViewer data={{ a: true, b: false }} />);
    expect(container.textContent).toContain("true");
    expect(container.textContent).toContain("false");
    expect(container.querySelectorAll(".text-sky-600").length).toBe(2);
  });

  // 测试目的：null 渲染为小写 null 而非空白。可能发现的缺陷：null 走 String(null) 之外的分支或被当作对象折叠。
  it("null 值渲染为 'null' 文本（不折叠为 {…}）", () => {
    const { container } = render(<LogDataViewer data={{ nil: null }} />);
    expect(container.textContent).toContain("null");
    expect(container.textContent).not.toContain("{…}");
  });

  // 测试目的：底部固定提示常驻。可能发现的缺陷：提示语缺失使用户误认为看到完整数据。
  it("非空对象渲染底部折叠说明提示", () => {
    render(<LogDataViewer data={{ a: 1 }} />);
    expect(screen.getByText(/嵌套对象\/数组已折叠预览/)).toBeTruthy();
  });
});

describe("LogDataViewer 嵌套结构折叠", () => {
  // 测试目的：嵌套对象折叠为 {…}，深层内容不泄露。可能发现的缺陷：递归无限展开导致日志过长/性能问题。
  it("嵌套对象折叠为 {…} 且不渲染深层键值", () => {
    const { container } = render(
      <LogDataViewer data={{ nested: { secret: "DEEP_VALUE", n: 1 } }} />,
    );
    expect(screen.getByText("{…}")).toBeTruthy();
    expect(container.textContent).not.toContain("DEEP_VALUE");
    expect(container.textContent).not.toContain("secret");
  });

  // 测试目的：数组折叠为 […]。可能发现的缺陷：Array.isArray 判断缺失，数组被渲染成 {…}。
  it("数组折叠为 […] 且不渲染元素", () => {
    const { container } = render(<LogDataViewer data={{ list: [1, 2, 3] }} />);
    expect(screen.getByText("[…]")).toBeTruthy();
    expect(container.textContent).not.toContain("{…}");
  });

  // 测试目的：空对象/空数组同样按嵌套折叠处理。可能发现的缺陷：空集合被当标量走 String() 渲染成 "[object Object]"。
  it("空对象与空数组分别折叠为 {…} 与 […]（不出现 [object Object]）", () => {
    const { container } = render(<LogDataViewer data={{ o: {}, a: [] }} />);
    expect(screen.getByText("{…}")).toBeTruthy();
    expect(screen.getByText("[…]")).toBeTruthy();
    expect(container.textContent).not.toContain("[object Object]");
  });

  // 测试目的：多层混合结构与标量共存时各自渲染正确。可能发现的缺陷：分支判断顺序错误导致标量被折叠。
  it("嵌套与标量混合时标量正常展示、嵌套折叠", () => {
    const { container } = render(
      <LogDataViewer
        data={{
          scalar: "visible",
          deep: { a: { b: { c: 1 } } },
          arr: [{ x: 1 }],
        }}
      />,
    );
    expect(container.textContent).toContain('"visible"');
    expect(screen.getByText("{…}")).toBeTruthy();
    expect(screen.getByText("[…]")).toBeTruthy();
    expect(container.textContent).not.toContain("c:");
  });
});

describe("LogDataViewer 边界与异常输入", () => {
  // 测试目的：空对象显示占位且不渲染底部提示。可能发现的缺陷：空态渲染空白容器造成 UI 塌陷。
  it("空对象显示 '（空对象）' 占位且无折叠提示", () => {
    render(<LogDataViewer data={{}} />);
    expect(screen.getByText("（空对象）")).toBeTruthy();
    expect(screen.queryByText(/嵌套对象\/数组已折叠预览/)).toBeNull();
  });

  // 测试目的：特殊数值（NaN/Infinity/0/-0）不崩溃并有可读输出。可能发现的缺陷：数值格式化异常输出空串。
  it("NaN / Infinity / 0 等特殊数值可读渲染", () => {
    const { container } = render(
      <LogDataViewer data={{ n: Number.NaN, inf: Number.POSITIVE_INFINITY, z: 0 }} />,
    );
    expect(container.textContent).toContain("NaN");
    expect(container.textContent).toContain("Infinity");
    expect(container.textContent).toContain("0");
  });

  // 测试目的：undefined 值不崩溃。可能发现的缺陷：undefined 走 String() 输出 "undefined" 或抛错。
  it("undefined 值渲染不崩溃", () => {
    expect(() =>
      render(<LogDataViewer data={{ u: undefined } as Record<string, unknown>} />),
    ).not.toThrow();
  });

  // 测试目的：空串键名与含特殊字符键名不破坏渲染。可能发现的缺陷：key 为空串导致 React key 冲突警告或漏渲染。
  it("空串键名与特殊字符键名均渲染", () => {
    const { container } = render(
      <LogDataViewer data={{ "": "empty-key", "a.b/c": 1, "中文键": true }} />,
    );
    expect(container.textContent).toContain("empty-key");
    expect(screen.getByText("a.b/c:")).toBeTruthy();
    expect(screen.getByText("中文键:")).toBeTruthy();
  });

  // 测试目的：循环引用对象因被折叠不触发无限递归。可能发现的缺陷：若改为深度展开会栈溢出。
  it("循环引用对象渲染不崩溃（依赖折叠预览）", () => {
    const cyclic: Record<string, unknown> = { name: "root" };
    cyclic.self = cyclic;
    expect(() => render(<LogDataViewer data={cyclic} />)).not.toThrow();
    expect(screen.getByText("{…}")).toBeTruthy();
  });

  // 测试目的：大量键（200）渲染完整不截断。可能发现的缺陷：内部做了隐式 slice 导致数据静默丢失。
  it("200 个键全部渲染不静默截断", () => {
    const data: Record<string, unknown> = {};
    for (let i = 0; i < 200; i += 1) data[`k${i}`] = i;
    const { container } = render(<LogDataViewer data={data} />);
    expect(container.querySelectorAll(".text-violet-600").length).toBe(200);
    expect(screen.getByText("k199:")).toBeTruthy();
  });

  // 测试目的：多行字符串与含引号字符串原样渲染。可能发现的缺陷：转义处理不当破坏文本。
  it("含引号与换行的字符串原样渲染", () => {
    const { container } = render(
      <LogDataViewer data={{ s: 'he said "hi"', multi: "l1\nl2" }} />,
    );
    expect(container.textContent).toContain('he said "hi"');
    expect(container.textContent).toContain("l1");
    expect(container.textContent).toContain("l2");
  });
});
