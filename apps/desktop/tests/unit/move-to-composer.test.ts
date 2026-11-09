import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));

import { readComposerDraft, taskDraftKey } from "@/lib/assistant/composer/composer-drafts";
import { moveMessageToComposer } from "@/lib/assistant/composer/move-to-composer";
import type { TransportMessage } from "@/lib/assistant/contract";
import { toThreadMessageWithRenderContext as toThreadMessage, type TransportMessageRenderContext } from "@/lib/assistant/transport-message-converter";
import { outcome } from "./transport-fixtures";

const context: TransportMessageRenderContext = { turn: 1, inherited: false, source: null, outcome: outcome("completed"), activity: null };
const imageLocator = `cosir-attachment://${"a".repeat(64)}`;

function message(id: string): TransportMessage {
  return {
    id,
    role: "user",
    turn: 1,
    inherited: false,
    source: null,
    outcome: null,
    parts: [
      { type: "text", text: `<file path="/tmp/${id}.md" />看 `, status: "completed" },
      { type: "image", image: imageLocator },
      { type: "text", text: '和 <ref path="src/a.py" lines="1-2" />', status: "completed" },
    ],
  };
}

describe("moveMessageToComposer", () => {
  it("撤回成功把正文胶囊与图片附件放回主输入框", async () => {
    const canonical = message("m1");
    const thread = toThreadMessage(canonical, context);
    if (thread.role !== "user") throw new Error("expected user message");
    const remove = vi.fn(async () => {});

    await moveMessageToComposer({ taskId: 99201, message: thread, canonical, remove });

    expect(remove).toHaveBeenCalledOnce();
    const draft = readComposerDraft(taskDraftKey(99201));
    expect(draft?.text).toBe('<file path="/tmp/m1.md" />看 和 <ref path="src/a.py" lines="1-2" />');
    expect(draft?.attachments.map((attachment) => attachment.id)).toEqual([imageLocator]);
  });

  it("撤回被拒时原样抛出，不动输入框", async () => {
    const canonical = message("m2");
    const thread = toThreadMessage(canonical, context);
    if (thread.role !== "user") throw new Error("expected user message");

    await expect(moveMessageToComposer({
      taskId: 99202, message: thread, canonical, remove: async () => { throw new Error("409"); },
    })).rejects.toThrow("409");

    expect(readComposerDraft(taskDraftKey(99202))).toBeUndefined();
  });
});
