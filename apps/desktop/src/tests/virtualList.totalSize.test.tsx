// @vitest-environment happy-dom
/**
 * VirtualList total size and dynamic row measurement regression tests.
 *
 * These tests lock the contract needed by ChatPanel: virtual rows are absolutely
 * positioned with translateY, so stale row-height cache can place later turns on
 * top of earlier growing content.
 */
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { VirtualList } from "@/lib/virtual/VirtualList";

/** Render a VirtualList with fixed estimated row height. */
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
  it("reports a positive total size for non-empty items", () => {
    const onTotalSizeChange = vi.fn();
    renderList(["a", "b", "c"], onTotalSizeChange, 100);

    expect(onTotalSizeChange).toHaveBeenCalled();
    for (const call of onTotalSizeChange.mock.calls) {
      expect(typeof call[0]).toBe("number");
      expect(call[0]).toBeGreaterThan(0);
    }
  });

  it("reports total size when items change from empty to non-empty", () => {
    const onTotalSizeChange = vi.fn();
    const { rerender } = renderList([], onTotalSizeChange);
    expect(onTotalSizeChange).not.toHaveBeenCalled();

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

  it("does not report total size for empty items", () => {
    const onTotalSizeChange = vi.fn();
    renderList([], onTotalSizeChange);
    expect(onTotalSizeChange).not.toHaveBeenCalled();
  });

  it("does not repeat total-size reports when size is unchanged", () => {
    const onTotalSizeChange = vi.fn();
    const { rerender } = renderList(["fixed"], onTotalSizeChange);
    expect(onTotalSizeChange).toHaveBeenCalledTimes(1);

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
