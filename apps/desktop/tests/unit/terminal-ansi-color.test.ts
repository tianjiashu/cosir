import { describe, expect, it } from "vitest";

import { ansiColor } from "@/components/terminal/xterm-appearance";

describe("ansiColor", () => {
  const theme = { red: "#f00", brightWhite: "#fff" };

  it("前 16 色跟主题，真彩色原样，其余按 256 色表换算", () => {
    expect(ansiColor(1, theme)).toBe("#f00");
    expect(ansiColor(15, theme)).toBe("#fff");
    expect(ansiColor("#0a141e", theme)).toBe("#0a141e");
    expect(ansiColor(16, theme)).toBe("rgb(0 0 0)");
    expect(ansiColor(208, theme)).toBe("rgb(255 135 0)");
    expect(ansiColor(232, theme)).toBe("rgb(8 8 8)");
  });
});
