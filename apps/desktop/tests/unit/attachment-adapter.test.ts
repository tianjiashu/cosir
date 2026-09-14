import { describe, expect, it } from "vitest";
import { fileMatchesAccept } from "@assistant-ui/core/internal";

import { createAttachmentAdapter } from "@/lib/assistant/attachments/image-attachment-adapter";
import { registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";

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
