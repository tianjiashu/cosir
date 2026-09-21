import { describe, expect, it } from "vitest";

import { toAddMessageCommand } from "@/lib/assistant/use-task-assistant-transport-runtime";

describe("toAddMessageCommand", () => {
  it("promotes uploaded image attachment content into transport image parts", () => {
    const locator = `cosir-attachment://${"a".repeat(64)}`;
    const message = {
      role: "user",
      content: [],
      attachments: [{
        id: locator,
        type: "image",
        name: "截图.png",
        contentType: "image/png",
        content: [{ type: "image", image: locator }],
      }],
    } as unknown as Parameters<typeof toAddMessageCommand>[0];

    expect(toAddMessageCommand(message).message.parts).toEqual([
      { type: "image", image: locator },
    ]);
  });

  it("does not duplicate an image already present in message content", () => {
    const locator = `cosir-attachment://${"b".repeat(64)}`;
    const message = {
      role: "user",
      content: [{ type: "image", image: locator }],
      attachments: [{
        id: locator,
        type: "image",
        name: "截图.png",
        contentType: "image/png",
        content: [{ type: "image", image: locator }],
      }],
    } as unknown as Parameters<typeof toAddMessageCommand>[0];

    expect(toAddMessageCommand(message).message.parts).toEqual([
      { type: "image", image: locator },
    ]);
  });
});
