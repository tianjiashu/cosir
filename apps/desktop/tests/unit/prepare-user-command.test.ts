import { describe, expect, it } from "vitest";

import { registerLocalFile, restoreLocalFile } from "@/lib/assistant/attachments/local-attachment-registry";
import { prepareUserCommand } from "@/lib/assistant/composer/prepare-user-command";

describe("prepare user command", () => {
  it("preserves ordinary-file tokens and adds explicit local attachment metadata", () => {
    const id = registerLocalFile({
      path: "C:\\workspace\\报告 final.md",
      name: "报告 final.md",
      contentType: "text/markdown",
    });
    const prepared = prepareUserCommand({
      type: "add-message",
      message: {
        role: "user",
        parts: [
          { type: "text", text: `读取 [[cosir-file:${id}]]` },
          { type: "image", image: "cosir-attachment://" + "a".repeat(64) },
        ],
      },
    });
    expect(prepared).toEqual({
      type: "add-message",
      message: {
        role: "user",
        parts: [
          { type: "text", text: `读取 [[cosir-file:${id}]]` },
          { type: "image", image: "cosir-attachment://" + "a".repeat(64) },
        ],
        attachments: [{
          id,
          name: "报告 final.md",
          contentType: "text/markdown",
          path: "C:\\workspace\\报告 final.md",
        }],
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

  it("放回输入框的历史附件按快照里的路径登记后，照常随新消息发送", () => {
    restoreLocalFile({ id: "history", name: "history.md", contentType: "text/markdown", path: "/w/history.md" });
    const prepared = prepareUserCommand({
      type: "add-message",
      message: { role: "user", parts: [{ type: "text", text: "[[cosir-file:history]]" }] },
    }) as { message: { attachments: unknown[] } };
    expect(prepared.message.attachments).toEqual([
      { id: "history", name: "history.md", contentType: "text/markdown", path: "/w/history.md" },
    ]);
  });

  it("keeps canonical hidden token wrappers in a restored message", () => {
    restoreLocalFile({ id: "hidden", name: "notes.md", contentType: "text/markdown", path: "/w/notes.md" });
    expect(prepareUserCommand({
      type: "add-message",
      message: {
        role: "user",
        parts: [{ type: "text", text: "请查看 <!-- [[cosir-file:hidden]] -->" }],
      },
    })).toMatchObject({
      message: {
        parts: [{ type: "text", text: "请查看 <!-- [[cosir-file:hidden]] -->" }],
      },
    });
  });
});
