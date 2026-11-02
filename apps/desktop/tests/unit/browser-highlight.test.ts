import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
import type { CdpSession } from "@/electron/main/browser/cdp-session";
import { TabHighlight } from "@/electron/main/browser/tab-highlight";

function fakeSession() {
  const calls: [string, Record<string, unknown> | undefined][] = [];
  const session = { send: vi.fn(async (method: string, params?: Record<string, unknown>) => {
    calls.push([method, params]);
    return method === "Runtime.evaluate" ? { result: { value: 2 } } : {};
  }) };
  return { session: session as unknown as CdpSession, calls };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("TabHighlight", () => {
  // 目的：框画在被操作的元素上；潜在缺陷：按 CSS 像素画，Retina 上框落到两倍处（真实应用验收发现）。
  it("draws in CSS pixels divided by the pixel ratio, then hides itself after a moment", async () => {
    const { session, calls } = fakeSession();
    const highlight = new TabHighlight(session, "t1", () => true);
    await highlight.show(async () => ({ x: 300, y: 400, width: 80, height: 20 }));
    expect(calls.map(([method]) => method)).toEqual(["Runtime.evaluate", "DOM.enable", "Overlay.enable", "Overlay.highlightQuad"]);
    expect(calls[3][1]?.quad).toEqual([150, 200, 190, 200, 190, 210, 150, 210]);
    await vi.advanceTimersByTimeAsync(600);
    expect(calls.at(-1)?.[0]).toBe("Overlay.hideHighlight");
  });

  // 目的：没人看时不白白量位置、开域；截图前收起。潜在缺陷：后台标签也去滚动页面量元素，或框进了 Agent 的截图。
  it("does nothing while the tab is not shown, and clears only what it drew", async () => {
    const { session, calls } = fakeSession();
    const locate = vi.fn(async () => ({ x: 0, y: 0, width: 1, height: 1 }));
    const hidden = new TabHighlight(session, "t1", () => false);
    await hidden.show(locate);
    await hidden.clear();
    expect(locate).not.toHaveBeenCalled();
    expect(calls).toEqual([]);
  });
});
