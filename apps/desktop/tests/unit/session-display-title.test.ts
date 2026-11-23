import { describe, expect, it } from "vitest";

import { sessionDisplayTitle, UNTITLED_SESSION_LABEL } from "@/lib/session-title/session-display-title";

describe("sessionDisplayTitle", () => {
  it("shows the stored title as is", () => {
    expect(sessionDisplayTitle("重构登录模块")).toBe("重构登录模块");
  });

  it("names an untitled session in one place; the backend stores no placeholder", () => {
    expect(sessionDisplayTitle("")).toBe(UNTITLED_SESSION_LABEL);
    expect(sessionDisplayTitle("   ")).toBe(UNTITLED_SESSION_LABEL);
  });
});
