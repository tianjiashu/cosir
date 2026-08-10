// @vitest-environment happy-dom
/**
 * VirtualList 真实总高度上报（onTotalSizeChange）契约测试。
 *
 * 背景 bug：ChatPanel 用滚动容器的 `scrollHeight` 做「滚到底部」，但虚拟列表内条目为
 * 绝对定位 + `translateY`，`scrollHeight` 由虚拟器 totalSize 撑起，而 totalSize 随单条
 * 动态测量（折叠/展开/流式）异步更新。测量滞后窗口内 `scrollHeight` 偏小 → 滚不到真底，
 * 且 smooth 动画在 totalSize 变化的瞬间与 translateY 重排相互打架 → 相邻 turn 短暂重叠。
 *
 * 修复契约：
 * 1. `VirtualList` 暴露 `onTotalSizeChange(totalSize)`，回调在真实总高度变化时触发，
 *    调用方据此滚底，而非依赖 `scrollHeight`。
 * 2. `items.length` 变化后主动 `virtualizer.measure()` 重排，避免首帧 translateY 错位
 *    导致的条目重叠。
 *
 * 本文件用固定 `estimateSize` + happy-dom（无真实布局，measure 返回 0）验证回调触发
 * 的关键不变量：空列表不上报、非空上报、不重复上报、上报值为非负数字。不依赖浏览器
 * 真实布局，保证契约在 CI 下稳定可测。
 */
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { VirtualList } from "@/lib/virtual/VirtualList";

/** 渲染一个固定 estimateSize 的 VirtualList。 */
function renderList(
  items: string[],
  onTotalSizeChange: ReturnType<typeof vi.fn>,
  estimateSize = 100,
) {
  return render(
    <VirtualList
      items={items}
      getKey={(item) => item}
      renderItem={(item) => <div data-testid="item">{item}</div>}
      estimateSize={estimateSize}
      onTotalSizeChange={onTotalSizeChange}
    />,
  );
}

describe("VirtualList.onTotalSizeChange", () => {
  it("items 非空时，回调在真实总高度上报中至少触发一次，且值为非负数字", () => {
    const onTotalSizeChange = vi.fn();
    renderList(["a", "b", "c"], onTotalSizeChange, 100);
    expect(onTotalSizeChange).toHaveBeenCalled();
    for (const call of onTotalSizeChange.mock.calls) {
      expect(typeof call[0]).toBe("number");
      // 非空列表上报的 totalSize 必须 > 0（契约：totalSize<=0 不上报，调用方滚底
      // 只需正高度）。不锁精确值：happy-dom 无真实布局，measureElement 返回 0，
      // 精确值受测试环境布局细节影响，锁「>0 + 数字」已足够锁定关键不变量。
      expect(call[0]).toBeGreaterThan(0);
    }
  });

  it("items 由空转非空后触发回调（初始 estimate 撑起的 totalSize 上报一次）", () => {
    const onTotalSizeChange = vi.fn();
    const { rerender } = renderList([], onTotalSizeChange);
    expect(onTotalSizeChange).not.toHaveBeenCalled(); // 空列表不上报

    rerender(
      <VirtualList
        items={["x", "y"]}
        getKey={(item) => item}
        renderItem={(item) => <div data-testid="item">{item}</div>}
        estimateSize={50}
        onTotalSizeChange={onTotalSizeChange}
      />,
    );
    expect(onTotalSizeChange).toHaveBeenCalledTimes(1);
    expect(onTotalSizeChange.mock.calls[0][0]).toBeGreaterThan(0);
  });

  it("空列表不触发回调（避免调用方用 0 高度误滚底）", () => {
    const onTotalSizeChange = vi.fn();
    renderList([], onTotalSizeChange);
    expect(onTotalSizeChange).not.toHaveBeenCalled();
  });

  it("totalSize 未变化时（items 不变重渲染）不重复触发回调", () => {
    const onTotalSizeChange = vi.fn();
    const { rerender } = renderList(["fixed"], onTotalSizeChange);
    expect(onTotalSizeChange).toHaveBeenCalledTimes(1);

    // 用相同的 estimateSize 重渲染同一 items（新数组引用），totalSize 不应变化 → 不重复上报。
    onTotalSizeChange.mockClear();
    rerender(
      <VirtualList
        items={["fixed"]}
        getKey={(item) => item}
        renderItem={(item) => <div data-testid="item">{item}</div>}
        estimateSize={100}
        onTotalSizeChange={onTotalSizeChange}
      />,
    );
    expect(onTotalSizeChange).not.toHaveBeenCalled();
  });
});
