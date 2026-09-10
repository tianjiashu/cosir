import { describe, expect, it } from "vitest";

import {
  formatTokenCount,
  getContextUsagePresentation,
  shouldDisplayRunUsage,
} from "@/components/assistant/usage-display";

describe("usage display", () => {
  it("formats token counts compactly without turning invalid values into numbers", () => {
    expect(formatTokenCount(0)).toBe("0");
    expect(formatTokenCount(12_345)).toBe("12k");
    expect(formatTokenCount(999_999)).toBe("1M");
    expect(formatTokenCount(1_234_567)).toBe("1.2M");
    expect(formatTokenCount(Number.NaN)).toBe("—");
  });

  it("keeps unknown, measured, warning, and overage context states distinct", () => {
    expect(getContextUsagePresentation(0, null, null)).toMatchObject({
      label: "上下文 —",
      measured: false,
      tone: "unknown",
    });
    expect(getContextUsagePresentation(0.7, 70_000, 100_000)).toMatchObject({
      label: "上下文 70%",
      detail: "70k / 100k tokens",
      tone: "warning",
      measured: true,
    });
    expect(getContextUsagePresentation(0.85, 85_000, 100_000).tone).toBe("warning");
    expect(getContextUsagePresentation(0.649, 64_900, 100_000).tone).toBe("normal");
    expect(getContextUsagePresentation(0.849, 84_900, 100_000).tone).toBe("warning");
    expect(getContextUsagePresentation(0.851, 85_100, 100_000).status).toBe("critical");
    expect(getContextUsagePresentation(1.2, 120_000, 100_000)).toMatchObject({
      percent: 120,
      status: "overage",
      overageTokens: 20_000,
      tone: "critical",
    });
    expect(getContextUsagePresentation(0.9, 90_000, 100_000).status).toBe("critical");
  });

  it("only attaches run usage to the matching latest assistant run", () => {
    expect(shouldDisplayRunUsage(true, 3, true)).toBe(true);
    expect(shouldDisplayRunUsage(true, 3, false)).toBe(false);
    expect(shouldDisplayRunUsage(false, 3, true)).toBe(false);
    expect(shouldDisplayRunUsage(true, null, true)).toBe(false);
  });
});
