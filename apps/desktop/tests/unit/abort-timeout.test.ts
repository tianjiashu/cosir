import { afterEach, describe, expect, it, vi } from "vitest";

import { createTimeoutAbort, isCancelledAbort, isTimeoutAbort } from "@/lib/async/abort-timeout";
import { safeFrontendErrorMessage } from "@/lib/logging/frontend-log";

describe("createTimeoutAbort", () => {
  afterEach(() => vi.useRealTimers());

  it("aborts with a TimeoutError reason so callers can treat it as a failure", () => {
    vi.useFakeTimers();
    const { signal } = createTimeoutAbort(100);
    vi.advanceTimersByTime(100);

    expect(signal.aborted).toBe(true);
    expect(isTimeoutAbort(signal)).toBe(true);
    expect(isCancelledAbort(signal)).toBe(false);
    expect(safeFrontendErrorMessage(signal.reason, "fallback")).toContain("超时");
  });

  it("treats an owner abort as cancellation, not timeout", () => {
    const { controller, signal, clear } = createTimeoutAbort(100);
    controller.abort();
    clear();

    expect(isTimeoutAbort(signal)).toBe(false);
    expect(isCancelledAbort(signal)).toBe(true);
    expect(safeFrontendErrorMessage(signal.reason, "fallback")).toBe("请求已取消");
  });

  it("does not abort after clear", () => {
    vi.useFakeTimers();
    const { signal, clear } = createTimeoutAbort(100);
    clear();
    vi.advanceTimersByTime(200);

    expect(signal.aborted).toBe(false);
  });
});
