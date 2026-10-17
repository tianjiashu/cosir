import { describe, expect, it } from "vitest";
import { fileMatchesAccept } from "@assistant-ui/core/internal";

import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";

describe("assistant attachment adapter", () => {
  it("接受图片恢复契约与空 MIME 的图片文件", () => {
    const adapter = createAttachmentAdapter(7);
    expect(fileMatchesAccept({ name: "photo.jpg", type: "image/jpeg" }, adapter.accept)).toBe(true);
    expect(fileMatchesAccept({ name: "image-1", type: "image/*" }, adapter.accept)).toBe(true);
    expect(fileMatchesAccept({ name: "native.png", type: "" }, adapter.accept)).toBe(true);
    expect(fileMatchesAccept({ name: "notes.md", type: "text/markdown" }, adapter.accept)).toBe(false);
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
    await expect(createAttachmentAdapter(7).add({ file })).rejects.toThrow("JPEG 和 PNG");
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

  it("普通文件不能重新进入图片附件集合", async () => {
    await expect(createAttachmentAdapter(7).add({ file: new File([], "notes.md", { type: "text/plain" }) }))
      .rejects.toThrow("JPEG 和 PNG");
  });
});
