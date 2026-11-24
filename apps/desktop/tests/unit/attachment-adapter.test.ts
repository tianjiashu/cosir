import { describe, expect, it, vi } from "vitest";
import { fileMatchesAccept } from "@assistant-ui/core/internal";
import type { PendingAttachment } from "@assistant-ui/core";

import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";
import { uploadAttachment } from "@/lib/assistant/attachments/attachment-upload";

vi.mock("@/lib/assistant/attachments/attachment-upload", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/assistant/attachments/attachment-upload")>()),
  uploadAttachment: vi.fn(),
}));

describe("assistant attachment adapter", () => {
  it("接受图片恢复契约与空 MIME 的图片文件", () => {
    const adapter = createAttachmentAdapter();
    expect(fileMatchesAccept({ name: "photo.jpg", type: "image/jpeg" }, adapter.accept)).toBe(true);
    expect(fileMatchesAccept({ name: "image-1", type: "image/*" }, adapter.accept)).toBe(true);
    expect(fileMatchesAccept({ name: "native.png", type: "" }, adapter.accept)).toBe(true);
    expect(fileMatchesAccept({ name: "notes.md", type: "text/markdown" }, adapter.accept)).toBe(false);
  });

  it("creates a pending image attachment for a JPEG file", async () => {
    const file = new File(["image"], "photo.jpg", { type: "image/jpeg" });
    const attachment = await createAttachmentAdapter().add({ file });

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
    await expect(createAttachmentAdapter().add({ file })).rejects.toThrow("JPEG 和 PNG");
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

    await expect(createAttachmentAdapter().remove(attachment)).resolves.toBeUndefined();
  });

  it("普通文件不能重新进入图片附件集合", async () => {
    await expect(createAttachmentAdapter().add({ file: new File([], "notes.md", { type: "text/plain" }) }))
      .rejects.toThrow("JPEG 和 PNG");
  });

  it("发送不上传：把文件登记为待上传图片，立即交出带同一 UI 身份的完整附件", async () => {
    const adapter = createAttachmentAdapter();
    const file = new File(["identical-image"], "same.png", { type: "image/png" });
    const first = await adapter.add({ file }) as PendingAttachment;
    const second = await adapter.add({ file }) as PendingAttachment;
    expect(first.id).not.toBe(second.id);

    const sent = await adapter.send(first);
    expect(sent).toMatchObject({ id: first.id, status: { type: "complete" }, content: [{ type: "image", image: expect.stringMatching(/^blob:/) }] });
    expect(uploadAttachment).not.toHaveBeenCalled();
  });
});
