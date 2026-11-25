import type { Virtualizer } from "@tanstack/react-virtual";
import { describe, expect, it, vi } from "vitest";

import { measureRenderedElement } from "@/lib/virtual/measure-rendered-element";

type Instance = Virtualizer<Element, Element>;

function setup(rendered: boolean, cached?: number, currentSize = 104) {
  const element = { checkVisibility: () => rendered } as unknown as Element;
  const instance = {
    options: { horizontal: false, useCachedMeasurements: false, getItemKey: (index: number) => `row-${index}`, estimateSize: () => 104 },
    itemSizeCache: new Map(cached === undefined ? [] : [["row-3", cached]]),
    indexFromElement: () => 3,
    getVirtualItems: () => [{ index: 3, size: currentSize }],
  } as unknown as Instance;
  // 隐藏时 ResizeObserver 报 0，显示时报真实高度。
  const entry = { borderBoxSize: [{ blockSize: rendered ? 91 : 0, inlineSize: 0 }] } as unknown as ResizeObserverEntry;
  return { element, instance, entry };
}

describe("measureRenderedElement", () => {
  it("采用渲染中的行的实测高度，并交给调用方记忆", () => {
    const { element, instance, entry } = setup(true);
    const onMeasured = vi.fn();
    expect(measureRenderedElement(element, entry, instance, onMeasured)).toBe(91);
    expect(onMeasured).toHaveBeenCalledWith(91);
  });

  // 目的：读数等于当前尺寸也算测过；潜在缺陷：TanStack 差值为 0 时不写缓存，这一行的尺寸之后跟着估计悄悄变、阅读位置跳走。
  it("读数恰好等于当前尺寸时记为已测量，不等时留给 resizeItem 按差值补偿", () => {
    const equal = setup(true, undefined, 91);
    measureRenderedElement(equal.element, equal.entry, equal.instance);
    expect(equal.instance.itemSizeCache.get("row-3")).toBe(91);
    const differ = setup(true);
    measureRenderedElement(differ.element, differ.entry, differ.instance);
    expect(differ.instance.itemSizeCache.has("row-3")).toBe(false);
  });

  it("行被隐藏时沿用缓存高度，不把 0 交给调用方", () => {
    const { element, instance, entry } = setup(false, 91);
    const onMeasured = vi.fn();
    expect(measureRenderedElement(element, entry, instance, onMeasured)).toBe(91);
    expect(onMeasured).not.toHaveBeenCalled();
  });

  it("隐藏期间才挂载、没有缓存的行用估计高度而不是 0", () => {
    const { element, instance, entry } = setup(false);
    expect(measureRenderedElement(element, entry, instance)).toBe(104);
  });
});
