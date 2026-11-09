import { describe, expect, it } from "vitest";
import type { CompleteAttachment } from "@assistant-ui/core";

import {
  createEditableUserDocument,
  editableDocumentAttachments,
} from "@/lib/assistant/composer/editable-user-document";

function file(id: string): CompleteAttachment {
  return {
    id,
    type: "file",
    name: `${id}.md`,
    contentType: "text/markdown",
    content: [{ type: "file", data: `/tmp/${id}.md`, filename: `${id}.md`, mimeType: "text/markdown" }],
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
  it("只把图片放进附件条，正文里的胶囊标签原样保留", () => {
    const document = createEditableUserDocument('before<file path="/tmp/a.md" />after', [
      file("a"),
      image("image-a"),
      file("b"),
    ]);

    expect(document.images.map((attachment) => attachment.id)).toEqual(["image-a"]);
    expect(document.text).toBe('before<file path="/tmp/a.md" />after');
  });

  it("keep the first occurrence of an image and preserve attachment order", () => {
    const first = image("a");
    const document = createEditableUserDocument("text", [first, image("a"), image("i"), file("f")]);

    expect(editableDocumentAttachments(document).map((attachment) => attachment.id)).toEqual(["a", "i"]);
  });
});
