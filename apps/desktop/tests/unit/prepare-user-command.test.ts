import { describe, expect, it } from "vitest";

import { registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";
import { prepareUserCommand } from "@/lib/assistant/prepare-user-command";

describe("prepare user command", () => {
  it("replaces ordinary-file tokens by registered paths and leaves only text/image parts", () => {
    registerLocalAttachment(new File([], "报告 final.md", { type: "text/markdown" }), {
      id: "local-file-prepare",
      path: "C:\\workspace\\报告 final.md",
      name: "报告 final.md",
      contentType: "text/markdown",
      kind: "file",
    });
    const prepared = prepareUserCommand({
      type: "add-message",
      message: {
        role: "user",
        parts: [
          { type: "text", text: "读取 [[cosir-file:local-file-prepare]]" },
          { type: "image", image: "cosir-attachment://" + "a".repeat(64) },
        ],
      },
    });
    expect(prepared).toEqual({
      type: "add-message",
      message: {
        role: "user",
        parts: [
          { type: "text", text: "读取 C:\\workspace\\报告 final.md" },
          { type: "image", image: "cosir-attachment://" + "a".repeat(64) },
        ],
      },
    });
  });

  it("rejects an unregistered ordinary-file token", () => {
    expect(() => prepareUserCommand({
      type: "add-message",
      message: {
        role: "user",
        parts: [{ type: "text", text: "读取 [[cosir-file:missing]]" }],
      },
    })).toThrow("附件已失效，请重新选择附件");
  });
});
