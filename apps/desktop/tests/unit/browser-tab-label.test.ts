import { describe, expect, it } from "vitest";

import { displayedAddress, tabLabel } from "@/lib/browser/tab-label";

describe("tabLabel", () => {
  // 目的：标签条、地址栏、工具卡片对同一个标签叫法一致；潜在缺陷：空白页在某处显示成 about:blank、在别处是「新标签页」。
  it("prefers the title, then the address, and calls a blank page a new tab", () => {
    expect(tabLabel("Shop", "https://shop.test/")).toBe("Shop");
    expect(tabLabel("", "https://shop.test/")).toBe("https://shop.test/");
    expect(tabLabel("", "about:blank")).toBe("新标签页");
    expect(displayedAddress("about:blank")).toBe("");
    expect(displayedAddress("https://shop.test/")).toBe("https://shop.test/");
  });
});
