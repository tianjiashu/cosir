// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AgentMessage } from "@/components/chat/AgentMessage";

let container: HTMLDivElement;
let root: Root;

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
 * 渲染 AgentMessage 到测试容器。
 *
 * 参数:
 *   content - Markdown 消息内容。
 *   streaming - 是否处于流式生成中。
 * 返回: Promise<void>，渲染提交后 resolve。
 * 异常: 不主动抛出；渲染期异常由 React 向上冒泡。
 * 副作用: 修改测试容器 DOM。
 */
async function renderMessage(content: string, streaming?: boolean): Promise<void> {
  await act(async () => {
    root.render(<AgentMessage content={content} streaming={streaming} />);
  });
}

/** 查询 caret 元素（流式光标以 aria-hidden + animate-pulse 标识）。 */
function queryCaret(scope: ParentNode = container): Element | null {
  return scope.querySelector('[aria-hidden="true"].animate-pulse');
}

describe("AgentMessage 流式 caret", () => {
  it("streaming=true 且内容非空时渲染 caret", async () => {
    await renderMessage("正在输出的内容", true);

    expect(queryCaret()).not.toBeNull();
  });

  it("streaming=false 时不渲染 caret", async () => {
    await renderMessage("已完成的内容", false);

    expect(queryCaret()).toBeNull();
  });

  it("段落结尾的内容把 caret 放进最后一个 <p>", async () => {
    await renderMessage("第一行\n\n第二行", true);

    const paragraphs = container.querySelectorAll("p");
    expect(paragraphs.length).toBeGreaterThanOrEqual(2);
    const lastParagraph = paragraphs[paragraphs.length - 1];
    expect(queryCaret(lastParagraph)).not.toBeNull();
    // caret 只出现一次，不应每个段落都挂
    expect(container.querySelectorAll('[aria-hidden="true"].animate-pulse')).toHaveLength(1);
  });

  it("代码块结尾的内容把 caret 放进 <pre>", async () => {
    await renderMessage("说明文字\n\n```ts\nconst a = 1;\n```", true);

    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(queryCaret(pre as Element)).not.toBeNull();
    // 段落不应再挂 caret
    const paragraphs = container.querySelectorAll("p");
    paragraphs.forEach((paragraph) => {
      expect(queryCaret(paragraph)).toBeNull();
    });
  });

  it("消息主体容器带 min-h-[1.5em] 与 min-w-0", async () => {
    await renderMessage("内容", false);

    const body = container.querySelector(".min-h-\\[1\\.5em\\]");
    expect(body).not.toBeNull();
    expect(body?.className).toContain("min-w-0");
  });
});
