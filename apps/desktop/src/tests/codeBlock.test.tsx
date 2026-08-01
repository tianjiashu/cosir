// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { CodeBlock } from "@/components/chat/CodeBlock";

let container: HTMLDivElement;
let root: Root;

/** 超过折叠阈值（12 行）的长代码样本。 */
const LONG = Array.from({ length: 20 }, (_, i) => `line ${i}`).join("\n");

/** 折叠展开按钮的文案。 */
const EXPAND_LABEL = "展开完整代码";

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

/**
 * 渲染 CodeBlock 到测试容器。
 *
 * 参数:
 *   props - CodeBlock 的属性（code 必填，streaming / isLastLeaf 可选）。
 * 返回: Promise<void>，渲染提交后 resolve。
 * 异常: 不主动抛出；渲染期异常由 React 向上冒泡。
 * 副作用: 修改测试容器 DOM。
 */
async function renderCodeBlock(props: {
  code: string;
  language?: string;
  streaming?: boolean;
  isLastLeaf?: boolean;
}): Promise<void> {
  await act(async () => {
    root.render(<CodeBlock {...props} />);
  });
}

/**
 * 按可见文本查找元素。
 *
 * 参数:
 *   text - 需要精确匹配（去空白后）的文本内容。
 * 返回: 命中的元素，未命中返回 null。
 * 异常: 不抛出。
 * 副作用: 无。
 */
function queryByText(text: string): Element | null {
  const nodes = Array.from(container.querySelectorAll("*"));
  return nodes.find((node) => node.textContent?.trim() === text) ?? null;
}

/** 查询流式光标元素（aria-hidden + animate-pulse 标识）。 */
function queryCaret(scope: ParentNode = container): Element | null {
  return scope.querySelector('[aria-hidden="true"].animate-pulse');
}

describe("CodeBlock 流式折叠", () => {
  it("streaming=true 且代码超过阈值时折叠并展示展开按钮", async () => {
    await renderCodeBlock({ code: LONG, streaming: true, isLastLeaf: true });

    expect(queryByText(EXPAND_LABEL)).not.toBeNull();
    expect(container.querySelector(".max-h-48")).not.toBeNull();
  });

  it("streaming=false 时即使代码超长也不折叠", async () => {
    await renderCodeBlock({ code: LONG, streaming: false, isLastLeaf: true });

    expect(queryByText(EXPAND_LABEL)).toBeNull();
    expect(container.querySelector(".max-h-48")).toBeNull();
  });

  it("点击展开按钮后取消折叠", async () => {
    await renderCodeBlock({ code: LONG, streaming: true, isLastLeaf: true });

    const button = queryByText(EXPAND_LABEL) as HTMLButtonElement | null;
    expect(button).not.toBeNull();
    await act(async () => {
      button?.click();
    });

    expect(queryByText(EXPAND_LABEL)).toBeNull();
    expect(container.querySelector(".max-h-48")).toBeNull();
  });

  it("超长内联路径的 code 元素带 break-words", async () => {
    await renderCodeBlock({ code: "a".repeat(200), streaming: false });

    expect(container.querySelector(".break-words")).not.toBeNull();
  });

  it("streaming=true 且为末块时在 <pre> 尾部渲染 caret", async () => {
    await renderCodeBlock({ code: LONG, streaming: true, isLastLeaf: true });

    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(queryCaret(pre as Element)).not.toBeNull();
  });

  it("恰好 12 行时（未超阈值）不折叠", async () => {
    const EXACTLY_12 = Array.from({ length: 12 }, (_, i) => `line ${i}`).join("\n");
    await renderCodeBlock({ code: EXACTLY_12, streaming: true, isLastLeaf: true });

    expect(queryByText(EXPAND_LABEL)).toBeNull();
  });

  it("恰好 13 行时（超阈值）折叠并展示展开按钮", async () => {
    const EXACTLY_13 = Array.from({ length: 13 }, (_, i) => `line ${i}`).join("\n");
    await renderCodeBlock({ code: EXACTLY_13, streaming: true, isLastLeaf: true });

    expect(queryByText(EXPAND_LABEL)).not.toBeNull();
  });

  it("单行 700 字符（超字符阈值）折叠并展示展开按钮", async () => {
    await renderCodeBlock({ code: "x".repeat(700), streaming: true, isLastLeaf: true });

    expect(queryByText(EXPAND_LABEL)).not.toBeNull();
  });
});
