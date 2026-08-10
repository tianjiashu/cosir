// @vitest-environment happy-dom
/**
 * VirtualList dynamic row measurement regression tests.
 *
 * ChatPanel renders turns as absolutely positioned virtual rows. When a mounted
 * turn grows after streaming/tool expansion, the next row must receive a new
 * translateY value from the real TanStack virtualizer, otherwise the UI can
 * overlap like the captured conversation screenshot.
 */
import { describe, expect, it, vi } from "vitest";
import { act, render, waitFor } from "@testing-library/react";
import { VirtualList } from "@/lib/virtual/VirtualList";

type ResizeObserverCallback = ConstructorParameters<typeof ResizeObserver>[0];

class TestResizeObserver {
  private static callbacks: ResizeObserverCallback[] = [];

  private readonly callback: ResizeObserverCallback;

  public constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    TestResizeObserver.callbacks.push(callback);
  }

  public observe(target: Element): void {
    this.callback([createResizeObserverEntry(target)] as ResizeObserverEntry[], this);
  }

  public unobserve(): void {}

  public disconnect(): void {}

  public static triggerAll(target: Element): void {
    for (const callback of TestResizeObserver.callbacks) {
      callback([createResizeObserverEntry(target)] as ResizeObserverEntry[], {} as ResizeObserver);
    }
  }

  public static reset(): void {
    TestResizeObserver.callbacks = [];
  }
}

function createResizeObserverEntry(target: Element): Partial<ResizeObserverEntry> {
  const rect = target.getBoundingClientRect();
  return {
    target,
    borderBoxSize: [
      {
        inlineSize: rect.width,
        blockSize: rect.height,
      } as ResizeObserverSize,
    ],
    contentRect: rect,
  };
}

describe("VirtualList dynamic row measurement", () => {
  it("updates later row translateY when a mounted row grows", async () => {
    const originalGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect;
    const originalOffsetHeight = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "offsetHeight",
    );
    const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");
    const originalResizeObserver = globalThis.ResizeObserver;
    const originalRequestAnimationFrame = globalThis.requestAnimationFrame;
    const originalCancelAnimationFrame = globalThis.cancelAnimationFrame;

    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get() {
        return 240;
      },
    });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
      configurable: true,
      get() {
        return 320;
      },
    });
    HTMLElement.prototype.getBoundingClientRect = function getBoundingClientRectMock() {
      const text = this.textContent ?? "";
      const height = text.includes("expanded") ? 140 : 50;
      return {
        x: 0,
        y: 0,
        top: 0,
        left: 0,
        right: 320,
        bottom: height,
        width: 320,
        height,
        toJSON: () => ({}),
      } as DOMRect;
    };
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());

    try {
      const { container, rerender } = render(
        <VirtualList
          items={[
            { id: "first", text: "compact" },
            { id: "second", text: "tail" },
          ]}
          getKey={(item) => item.id}
          renderItem={(item) => <div>{item.text}</div>}
          estimateSize={50}
        />,
      );

      await waitFor(() => {
        expect((container.querySelector('[data-index="1"]') as HTMLElement).style.transform).toBe(
          "translateY(50px)",
        );
      });

      rerender(
        <VirtualList
          items={[
            { id: "first", text: "expanded content" },
            { id: "second", text: "tail" },
          ]}
          getKey={(item) => item.id}
          renderItem={(item) => <div>{item.text}</div>}
          estimateSize={50}
        />,
      );
      act(() => {
        TestResizeObserver.triggerAll(container.querySelector('[data-index="0"]') as Element);
      });

      await waitFor(() => {
        expect((container.querySelector('[data-index="1"]') as HTMLElement).style.transform).toBe(
          "translateY(140px)",
        );
      });
    } finally {
      HTMLElement.prototype.getBoundingClientRect = originalGetBoundingClientRect;
      if (originalOffsetHeight) {
        Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
      }
      if (originalOffsetWidth) {
        Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
      }
      vi.stubGlobal("ResizeObserver", originalResizeObserver);
      vi.stubGlobal("requestAnimationFrame", originalRequestAnimationFrame);
      vi.stubGlobal("cancelAnimationFrame", originalCancelAnimationFrame);
      TestResizeObserver.reset();
    }
  });
});
