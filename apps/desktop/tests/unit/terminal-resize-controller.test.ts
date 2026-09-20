import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TerminalResizeController } from "@/components/terminal/terminal-resize-controller";

describe("TerminalResizeController", () => {
  let previousWindow: Window | undefined;
  let frames: Array<FrameRequestCallback | undefined>;

  beforeEach(() => {
    previousWindow = globalThis.window;
    frames = [];
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: {
        requestAnimationFrame: (callback: FrameRequestCallback) => {
          frames.push(callback);
          return frames.length;
        },
        cancelAnimationFrame: (handle: number) => {
          frames[handle - 1] = undefined;
        },
        setTimeout: (callback: () => void) => {
          callback();
          return 1;
        },
        clearTimeout: () => undefined,
      },
    });
  });

  afterEach(() => {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: previousWindow,
    });
  });

  it("coalesces identical dimensions and skips zero-sized hosts", () => {
    let width = 0;
    const container = {
      getBoundingClientRect: () => ({ width, height: 100 }),
    } as HTMLElement;
    const fit = vi.fn();
    const controller = new TerminalResizeController(container, fit);

    frames.shift()?.(0);
    expect(fit).not.toHaveBeenCalled();

    width = 800;
    controller.request();
    frames.shift()?.(0);
    expect(fit).toHaveBeenCalledOnce();

    controller.request();
    frames.shift()?.(0);
    expect(fit).toHaveBeenCalledOnce();

    controller.dispose();
  });

  it("does not fit after disposal", () => {
    const container = {
      getBoundingClientRect: () => ({ width: 800, height: 100 }),
    } as HTMLElement;
    const fit = vi.fn();
    const controller = new TerminalResizeController(container, fit);
    controller.dispose();
    frames.shift()?.(0);

    expect(fit).not.toHaveBeenCalled();
  });

  it("keeps the dimension retryable when fit fails", () => {
    const container = {
      getBoundingClientRect: () => ({ width: 800, height: 100 }),
    } as HTMLElement;
    const fit = vi.fn(() => {
      if (fit.mock.calls.length === 1) throw new Error("font metrics not ready");
      return true;
    });
    const controller = new TerminalResizeController(container, fit);

    expect(() => frames.shift()?.(0)).not.toThrow();
    controller.request();
    frames.shift()?.(0);

    expect(fit).toHaveBeenCalledTimes(2);
    controller.dispose();
  });
});
