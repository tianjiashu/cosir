import { describe, expect, it, vi } from "vitest";

import { captureScreenshot } from "@/electron/main/browser/page-capture";

/** 可视视口滚到 (0, 500)、1280×800 CSS 像素，屏幕倍率 2：与真实的 `Page.getLayoutMetrics` 同一形状。 */
function fakeSend() {
  const send = vi.fn(async (...[method]: [method: string, params?: Record<string, unknown>]) => {
    if (method === "Page.getLayoutMetrics") {
      return {
        cssVisualViewport: { pageX: 0, pageY: 500, clientWidth: 1280, clientHeight: 800, scale: 1 },
        layoutViewport: { clientWidth: 2560 },
        cssLayoutViewport: { clientWidth: 1280 },
        cssContentSize: { width: 1280, height: 6000 },
      };
    }
    return { data: "AAAA" };
  });
  const clip = () => (send.mock.calls.find(([method]) => method === "Page.captureScreenshot")![1] as unknown as { clip: Record<string, number>; captureBeyondViewport: boolean });
  return { send: send as never, clip, calls: send };
}

describe("captureScreenshot 截区", () => {
  // 目的：区域就是 Agent 给的那一块；潜在缺陷：沿用元素截图的边距，或忘了加滚动，截到别处。
  it("captures a viewport region in document coordinates without a margin", async () => {
    const { send, clip } = fakeSend();
    await captureScreenshot(send, "jpeg", { region: { x: 100, y: 40, width: 400, height: 300 } });
    expect(clip()).toMatchObject({ clip: { x: 100, y: 540, width: 400, height: 300, scale: 0.5 }, captureBeyondViewport: false });
  });

  // 目的：小区域放大看清；潜在缺陷：小图按原尺寸截，模型看不清图标或数字。
  it("enlarges a small region by the element rule", async () => {
    const { send, clip } = fakeSend();
    await captureScreenshot(send, "jpeg", { region: { x: 0, y: 0, width: 80, height: 20 } });
    expect(clip().clip.scale).toBe(4 * 0.5);
    const { send: other, clip: otherClip } = fakeSend();
    await captureScreenshot(other, "jpeg", { region: { x: 0, y: 0, width: 400, height: 80 } });
    expect(otherClip().clip.scale).toBe(2 * 0.5);
  });

  // 目的：越界就报错而不是裁剪；潜在缺陷：裁剪后的图与 Agent 给的坐标对不上，又没有提示。
  it.each([
    { x: 1200, y: 0, width: 100, height: 10 },
    { x: 0, y: 700, width: 10, height: 101 },
  ])("rejects a region reaching outside the viewport and sends no capture", async (region) => {
    const { send, calls } = fakeSend();
    await expect(captureScreenshot(send, "jpeg", { region })).rejects.toMatchObject({ code: "OUT_OF_VIEWPORT" });
    expect(calls.mock.calls.map(([method]) => method)).toEqual(["Page.getLayoutMetrics"]);
  });

  // 目的：改截区不波及其他三种截法；潜在缺陷：区域分支改动了视口截图或冻结帧的缩放。
  it("keeps the viewport, element and freeze-frame captures as they were", async () => {
    const viewport = fakeSend();
    await captureScreenshot(viewport.send, "jpeg", {});
    expect(viewport.clip()).toMatchObject({ clip: { x: 0, y: 500, width: 1280, height: 800, scale: 0.5 }, captureBeyondViewport: false });
    const element = fakeSend();
    await captureScreenshot(element.send, "jpeg", { element: { x: 100, y: 40, width: 400, height: 300 } });
    expect(element.clip()).toMatchObject({ clip: { x: 92, y: 532, width: 416, height: 316, scale: 0.5 }, captureBeyondViewport: true });
    const freeze = fakeSend();
    await captureScreenshot(freeze.send, "png", { devicePixels: true });
    expect(freeze.clip().clip.scale).toBe(1);
  });
});
