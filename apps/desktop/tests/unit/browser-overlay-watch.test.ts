import { describe, expect, it } from "vitest";

import { coversTarget, hasVisibleContent, isCovered, overlayRoots, type OverlayElement, type Rect } from "@/lib/browser/overlay-watch";

function rect(left: number, top: number, right: number, bottom: number): Rect {
  return { left, top, right, bottom };
}

const EMPTY = rect(0, 0, 0, 0);

function element(box: Rect, children: OverlayElement[] = [], extra: Partial<OverlayElement> = {}): OverlayElement {
  return { tagName: "DIV", children, getBoundingClientRect: () => box, ...extra };
}

/** 浏览器占位区域：工作台在窗口右侧。 */
const SLOT = rect(600, 100, 1000, 700);

describe("overlay detection", () => {
  // 目的：portal 容器常是 0×0 包装层；潜在缺陷：只看容器矩形，菜单盖住网页也发现不了。
  it("looks inside zero-size portal wrappers", () => {
    const menu = element(EMPTY, [element(EMPTY, [element(rect(650, 120, 800, 300))])]);
    expect(coversTarget(menu, SLOT)).toBe(true);
    expect(hasVisibleContent(menu)).toBe(true);
  });

  // 目的：只在真正盖到网页时冻结；潜在缺陷：侧栏里的 Tooltip 也让网页区域闪成静态图。
  it("ignores overlays that do not touch the slot", () => {
    const tooltip = element(rect(10, 10, 120, 40));
    expect(isCovered(SLOT, [tooltip])).toBe(false);
    expect(isCovered(SLOT, [tooltip, element(rect(0, 0, 1200, 800))])).toBe(true);
  });

  it("treats empty persistent containers as no overlay", () => {
    const container = element(EMPTY, [element(EMPTY)]);
    expect(hasVisibleContent(container)).toBe(false);
    expect(coversTarget(element(EMPTY, [element(EMPTY, [element(EMPTY, [element(EMPTY, [element(SLOT)])])])]), SLOT)).toBe(false);
  });

  it("collects body portals except the app root, plus toasts", () => {
    const root = element(rect(0, 0, 1200, 800), [], { id: "root" });
    const portal = element(EMPTY);
    const script = element(EMPTY, [], { tagName: "SCRIPT" });
    const toast = element(rect(700, 600, 1000, 680));
    const toaster = element(EMPTY, [toast]);
    expect(overlayRoots(element(EMPTY, [root, portal, script]), [toaster])).toEqual([portal, toast]);
  });
});
