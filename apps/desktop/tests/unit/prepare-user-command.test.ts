import { describe, expect, it } from "vitest";

import { registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";
import { prepareUserCommand } from "@/lib/assistant/prepare-user-command";

describe("prepare user command", () => {
  it("preserves ordinary-file tokens and adds explicit local attachment metadata", () => {
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
          { type: "text", text: "读取 [[cosir-file:local-file-prepare]]" },
          { type: "image", image: "cosir-attachment://" + "a".repeat(64) },
        ],
        attachments: [{
          id: "local-file-prepare",
          name: "报告 final.md",
          contentType: "text/markdown",
          path: "C:\\workspace\\报告 final.md",
        }],
      },
    });
  });

  it("leaves an unresolved token in an edit for backend recovery", () => {
    expect(prepareUserCommand({
      type: "add-message",
      sourceId: "user-1",
      message: {
        role: "user",
        parts: [{ type: "text", text: "读取 [[cosir-file:missing]]" }],
      },
    })).toEqual({
      type: "add-message",
      sourceId: "user-1",
      message: {
        role: "user",
        parts: [{ type: "text", text: "读取 [[cosir-file:missing]]" }],
      },
    });
  });

  it("rejects a new message whose file token is no longer registered", () => {
    expect(() => prepareUserCommand({
      type: "add-message",
      message: {
        role: "user",
        parts: [{ type: "text", text: "读取 [[cosir-file:missing]]" }],
      },
    })).toThrow("附件已失效，请重新选择附件");
  });

  it("keeps canonical hidden token wrappers in an edit request", () => {
    expect(prepareUserCommand({
      type: "add-message",
      sourceId: "user-1",
      message: {
        role: "user",
        parts: [{ type: "text", text: "请查看 <!-- [[cosir-file:missing]] -->" }],
      },
    })).toMatchObject({
      message: {
        parts: [{ type: "text", text: "请查看 <!-- [[cosir-file:missing]] -->" }],
      },
    });
  });
});
