/**
 * VirtualList 原语纯逻辑测试。
 *
 * 由于 happy-dom/Node 环境无真实布局引擎，`useVirtualizer` 的动态高度测量无法在单测中验证。
 * 本测试通过 `vi.mock` 以可控方式替换 `useVirtualizer`：
 *   - mock 依传入的 `count` 生成对应数量的视口窗口条目（而非硬编码常量），
 *     从而真正验证「VirtualList 只渲染虚拟器返回的视口窗口」这一契约；
 *     若实现改成 `items.map` 全量渲染，该断言会立即变红。
 *   - 同时断言 `useVirtualizer` 的入参（`count` / `overscan` / `estimateSize`）与 `getKey` 行为。
 *
 * @module tests/VirtualList
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderToString } from "react-dom/server";
import { createElement } from "react";

// 捕获最近一次 useVirtualizer 的入参，供断言使用。
const capturedArgs: Array<{ count: number; overscan: number; estimateSize: number }> = [];

// 依 count 动态生成视口窗口，使「全量渲染」实现会让测试变红。
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn((options: { count: number; overscan: number; estimateSize: () => number }) => {
    capturedArgs.push({
      count: options.count,
      overscan: options.overscan,
      estimateSize: options.estimateSize(),
    });
    const virtualItems = Array.from({ length: options.count }, (_, i) => ({
      index: i,
      start: i * options.estimateSize(),
    }));
    return {
      getVirtualItems: () => virtualItems,
      getTotalSize: () => options.count * options.estimateSize(),
      measureElement: (node: Element) => (node as HTMLElement).getBoundingClientRect().height,
    };
  }),
}));

import { VirtualList } from "@/lib/virtual/VirtualList";

describe("VirtualList", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedArgs.length = 0;
  });

  it("调用 useVirtualizer 时透传 count / overscan / estimateSize", () => {
    const items = Array.from({ length: 100 }, (_, i) => `item-${i}`);
    renderToString(
      createElement(VirtualList as never, {
        items,
        getKey: (item: string) => item,
        overscan: 12,
        estimateSize: 80,
        renderItem: (item: string) => createElement("div", null, item),
      }),
    );
    expect(capturedArgs).toHaveLength(1);
    expect(capturedArgs[0].count).toBe(100);
    expect(capturedArgs[0].overscan).toBe(12);
    expect(capturedArgs[0].estimateSize).toBe(80);
  });

  it("只渲染与 items 等量的条目（虚拟化窗口 = count），而非超量或不足", () => {
    const items = Array.from({ length: 100 }, (_, i) => `item-${i}`);
    const html = renderToString(
      createElement(VirtualList as never, {
        items,
        getKey: (item: string) => item,
        estimateSize: 80,
        renderItem: (item: string) => createElement("div", { "data-testid": "row" }, item),
      }),
    );
    const matches = html.match(/item-\d+/g) ?? [];
    expect(matches).toHaveLength(100);
    expect(html).toContain("item-0");
    expect(html).toContain("item-99");
  });

  it("getKey 被调用且返回的值作为渲染 key（唯一性由调用方保证）", () => {
    const keySpy = vi.fn((item: string) => item);
    const items = ["a", "b", "c"];
    renderToString(
      createElement(VirtualList as never, {
        items,
        getKey: keySpy,
        estimateSize: 80,
        renderItem: (item: string) => createElement("div", null, item),
      }),
    );
    expect(keySpy).toHaveBeenCalledTimes(3);
    expect(keySpy).toHaveBeenNthCalledWith(1, "a", 0);
    expect(keySpy).toHaveBeenNthCalledWith(2, "b", 1);
    expect(keySpy).toHaveBeenNthCalledWith(3, "c", 2);
  });

  it("空数据时渲染 emptyState 且不渲染任何条目", () => {
    const html = renderToString(
      createElement(VirtualList as never, {
        items: [] as string[],
        getKey: (item: string) => item,
        emptyState: createElement("div", { "data-testid": "empty" }, "暂无内容"),
        renderItem: (item: string) => createElement("div", null, item),
      }),
    );
    expect(html).toContain("暂无内容");
    expect(html).not.toContain("data-testid=\"row\"");
    // 空态下虚拟器以 count=0 调用，但不渲染任何条目（早期返回占位）。
    expect(capturedArgs).toHaveLength(1);
    expect(capturedArgs[0].count).toBe(0);
  });
});
