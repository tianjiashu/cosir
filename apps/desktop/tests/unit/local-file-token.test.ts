import { describe, expect, it } from "vitest";

import {
  inlineAttachmentKey,
  inlineAttachmentTokenId,
  stripInlineAttachmentTokens,
} from "@/lib/assistant/attachments/local-file-token";

describe("inline attachment identity", () => {
  it("keeps file and image identities in one key format", () => {
    expect(inlineAttachmentKey("file", "a")).toBe("file:a");
    expect(inlineAttachmentKey("image", "b")).toBe("image:b");
  });

  it("normalizes a lifted file copy to its canonical local file id", () => {
    expect(
      inlineAttachmentTokenId({ id: "canonical", content: [{ type: "file", data: "cosir-local-file:real" }] }),
    ).toBe("real");
    // assistant-ui 的 lift 副本：id 是随机新值，locator 才是身份。
    expect(
      inlineAttachmentTokenId({ id: "lift-copy", content: [{ type: "file", data: "cosir-local-file:real" }] }),
    ).toBe("real");
  });

  it("normalizes an uploaded image copy to its attachment locator id", () => {
    expect(
      inlineAttachmentTokenId({ id: "lift-copy", content: [{ type: "image", image: "cosir-attachment://abc" }] }),
    ).toBe("abc");
  });

  it("falls back to the attachment id when no controlled locator is present", () => {
    expect(inlineAttachmentTokenId({ id: "no-locator" })).toBe("no-locator");
    expect(
      inlineAttachmentTokenId({ id: "foreign", content: [{ type: "file", data: "https://example.com/a.md" }] }),
    ).toBe("foreign");
  });
});

describe("strip inline attachment tokens", () => {
  it("removes bare tokens and collapses the gap they leave", () => {
    expect(stripInlineAttachmentTokens("[[cosir-file:a]]帮我优化这个文档")).toBe("帮我优化这个文档");
    expect(stripInlineAttachmentTokens("前文[[cosir-file:a]]后文")).toBe("前文 后文");
    expect(stripInlineAttachmentTokens("前文 <!-- [[cosir-image:b]] --> 后文")).toBe("前文 后文");
  });

  it("returns an empty string when nothing visible remains", () => {
    expect(stripInlineAttachmentTokens("[[cosir-file:a]][[cosir-image:b]]")).toBe("");
    expect(stripInlineAttachmentTokens("   ")).toBe("");
  });

  it("keeps unrelated text untouched", () => {
    expect(stripInlineAttachmentTokens("普通标题")).toBe("普通标题");
  });
});
