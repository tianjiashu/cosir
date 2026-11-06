import { describe, expect, it } from "vitest";
import { messagePreview } from "@/lib/assistant/message-preview";

describe("Agent message original-text excerpt", () => {
  it("keeps readable text and link labels, skipping code, images, tables and destinations", () => {
    expect(messagePreview("**核对** [取消入口](https://private.example) `stop`。\n\n```ts\nsecret()\n```\n\n![截图](https://image.example)\n\n| key | value |\n| --- | --- |\n| private | detail |\n\n后续说明。"))
      .toBe("核对 取消入口 stop。 后续说明。");
  });
  it("handles empty and unfinished Markdown without inventing a summary", () => {
    expect(messagePreview("```ts\nonly code")).toBe("");
    expect(messagePreview("**正在检查")).toContain("正在检查");
    expect(messagePreview("原文".repeat(1000))).toHaveLength(180);
  });
});
