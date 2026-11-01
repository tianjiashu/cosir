import { describe, expect, it } from "vitest";

import { elapsedSince, formatDuration } from "@/lib/assistant/run-duration";

describe("formatDuration", () => {
  it("秒、分秒、时分三档；不足 1 秒与负值按 0 秒", () => {
    expect(formatDuration(0)).toBe("0 秒");
    expect(formatDuration(999)).toBe("0 秒");
    expect(formatDuration(-5000)).toBe("0 秒");
    expect(formatDuration(48_000)).toBe("48 秒");
    expect(formatDuration(192_000)).toBe("3 分 12 秒");
    expect(formatDuration(3_720_000)).toBe("1 小时 02 分");
  });
});

describe("elapsedSince", () => {
  const now = Date.parse("2026-10-27T00:00:10.000Z");

  it("按 ISO 开始时间算已过去的毫秒数，时钟回拨按 0", () => {
    expect(elapsedSince("2026-10-27T00:00:00.000Z", now)).toBe(10_000);
    expect(elapsedSince("2026-10-27T00:00:30.000Z", now)).toBe(0);
  });

  it("时间缺失或无法解析时为 null，调用方不显示耗时", () => {
    expect(elapsedSince(null, now)).toBeNull();
    expect(elapsedSince(undefined, now)).toBeNull();
    expect(elapsedSince("not a date", now)).toBeNull();
  });
});
