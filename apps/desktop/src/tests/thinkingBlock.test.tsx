// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ThinkingBlock } from "@/components/chat/ThinkingBlock";

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
 * 渲染 ThinkingBlock 到测试容器。
 *
 * 参数:
 *   content - 思考内容文本；streaming - 是否流式生成中。
 * 返回: Promise<void>，渲染提交后 resolve。
 * 异常: 不主动抛出。
 * 副作用: 修改测试容器 DOM。
 */
async function renderBlock(content: string, streaming?: boolean): Promise<void> {
  await act(async () => {
    root.render(<ThinkingBlock content={content} streaming={streaming} />);
  });
}

/** 查找「深度思考」折叠切换按钮，不存在则返回 undefined。 */
function findToggle(): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find((item) =>
    item.textContent?.includes("深度思考"),
  );
}

describe("ThinkingBlock 流式行为", () => {
  it("streaming=true 时直接展示累积内容与 caret，且不渲染折叠栏", async () => {
    await renderBlock("正在思考的中间过程", true);

    expect(container.textContent).toContain("正在思考的中间过程");
    expect(container.querySelector('[aria-hidden="true"].animate-pulse')).not.toBeNull();
    expect(findToggle()).toBeUndefined();
    expect(container.textContent).not.toContain("深度思考");
  });

  it("streaming=false 时默认折叠，点击「深度思考」后展开内容", async () => {
    await renderBlock("被折叠的思考内容", false);

    expect(container.textContent).not.toContain("被折叠的思考内容");
    const toggle = findToggle();
    expect(toggle).toBeDefined();

    await act(async () => {
      toggle?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(container.textContent).toContain("被折叠的思考内容");
    // 非流式期不应出现流式光标
    expect(container.querySelector('[aria-hidden="true"].animate-pulse')).toBeNull();
  });

  it("内容为纯空白时不渲染任何元素", async () => {
    await renderBlock("   ", true);

    expect(container.textContent).toBe("");
  });
});
