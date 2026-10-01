import { describe, expect, it } from "vitest";
import type { PendingAttachment } from "@assistant-ui/core";
import { fileMatchesAccept } from "@assistant-ui/core/internal";

import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";
import { registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";

function assertPendingAttachment(
  attachment: PendingAttachment | AsyncGenerator<PendingAttachment, void>,
): asserts attachment is PendingAttachment {
  if (!("type" in attachment)) {
    throw new Error("测试适配器返回了异步附件生成器，而不是待发送附件");
  }
}

describe("assistant attachment adapter", () => {
  it("uses assistant-ui's all-file wildcard", () => {
    const adapter = createAttachmentAdapter(7);
    expect(adapter.accept).toBe("*");
    expect(fileMatchesAccept({ name: "photo.jpg", type: "image/jpeg" }, adapter.accept)).toBe(true);
    expect(fileMatchesAccept({ name: "notes.md", type: "text/markdown" }, adapter.accept)).toBe(true);
  });

  it("creates a pending image attachment for a JPEG file", async () => {
    const file = new File(["image"], "photo.jpg", { type: "image/jpeg" });
    const attachment = await createAttachmentAdapter(7).add({ file });

    expect(attachment).toMatchObject({
      type: "image",
      name: "photo.jpg",
      contentType: "image/jpeg",
      file,
      status: { type: "requires-action", reason: "composer-send" },
    });
  });

  it("rejects unsupported GIF image uploads before the HTTP request", async () => {
    const file = new File(["image"], "animation.gif", { type: "image/gif" });
    const attachment = await createAttachmentAdapter(7).add({ file });
    assertPendingAttachment(attachment);

    expect(attachment.type).toBe("file");
    await expect(createAttachmentAdapter(7).send(attachment)).rejects.toThrow("JPEG 和 PNG");
  });

  it("removes only the Composer reference without deleting the workspace asset", async () => {
    const attachment = {
      id: "a".repeat(64),
      type: "image" as const,
      name: "photo.png",
      contentType: "image/png",
      status: { type: "complete" as const },
      content: [{ type: "image" as const, image: `cosir-attachment://${"a".repeat(64)}` }],
    };

    await expect(createAttachmentAdapter(7).remove(attachment)).resolves.toBeUndefined();
  });

  it("reuses the local registry ID for ordinary file attachment tokens", async () => {
    const file = registerLocalAttachment(
      new File([], "leftHook.yml", { type: "application/yaml" }),
      {
        id: "local-left-hook",
        path: "C:\\workspace\\leftHook.yml",
        name: "leftHook.yml",
        contentType: "application/yaml",
        kind: "file",
      },
    );

    await expect(createAttachmentAdapter(7).add({ file })).resolves.toMatchObject({
      id: "local-left-hook",
      type: "file",
      name: "leftHook.yml",
    });
  });
});
