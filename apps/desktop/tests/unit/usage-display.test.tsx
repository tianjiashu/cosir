import { describe, expect, it } from "vitest";

import { getCacheHitRate } from "@/components/assistant/usage-display";

describe("getCacheHitRate", () => {
  it("calculates the cache hit rate from hit and miss tokens", () => {
    expect(getCacheHitRate(1_423_872, 10_225)).toBe(99.3);
  });

  it("returns null when cache miss data is unavailable", () => {
    expect(getCacheHitRate(1_423_872, null)).toBeNull();
  });

  it("returns null when there are no measured cache tokens", () => {
    expect(getCacheHitRate(0, 0)).toBeNull();
  });

  it("rejects invalid or negative token counts", () => {
    expect(getCacheHitRate(-1, 10)).toBeNull();
    expect(getCacheHitRate(Number.NaN, 10)).toBeNull();
  });
});
