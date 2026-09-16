import { describe, expect, it } from "vitest";
import type { CompleteAttachment } from "@assistant-ui/core";

import {
  createEditableUserDocument,
  editableDocumentAttachments,
} from "@/lib/assistant/editable-user-document";

function file(id: string): CompleteAttachment {
  return {
    id,
    type: "file",
    name: `${id}.md`,
    contentType: "text/markdown",
    content: [{
      type: "file",
      data: `cosir-local-file:${id}`,
      filename: `${id}.md`,
      mimeType: "text/markdown",
    }],
    status: { type: "complete" },
  };
}

function image(id: string): CompleteAttachment {
  return {
    id,
    type: "image",
    name: `${id}.png`,
    contentType: "image/png",
    content: [{ type: "image", image: id }],
    status: { type: "complete" },
  };
}

describe("editable user document", () => {
  it("splits ordinary files and images into their dedicated surfaces", () => {
    const document = createEditableUserDocument("before[[cosir-file:a]]after", [
      file("a"),
      image("image-a"),
      file("b"),
    ]);

    expect(document.inlineFiles.map((attachment) => attachment.id)).toEqual(["a", "b"]);
    expect(document.previewImages.map((attachment) => attachment.id)).toEqual(["image-a"]);
    expect(document.text).toBe("before[[cosir-file:a]]after");
  });

  it("keeps the first occurrence of an attachment ID and preserves file order", () => {
    const first = file("a");
    const document = createEditableUserDocument("text", [first, file("a"), image("i"), image("i")]);

    expect(editableDocumentAttachments(document).map((attachment) => attachment.id)).toEqual(["a", "i"]);
  });
});
