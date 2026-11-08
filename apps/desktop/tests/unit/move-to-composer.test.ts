import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));

import { getLocalAttachmentById } from "@/lib/assistant/attachments/local-attachment-registry";
import { readComposerDraft, taskDraftKey } from "@/lib/assistant/composer/composer-drafts";
import { moveMessageToComposer } from "@/lib/assistant/composer/move-to-composer";
import type { TransportMessage } from "@/lib/assistant/contract";
import { toThreadMessageWithRenderContext as toThreadMessage, type TransportMessageRenderContext } from "@/lib/assistant/transport-message-converter";
import { outcome } from "./transport-fixtures";

const context: TransportMessageRenderContext = { turn: 1, inherited: false, source: null, outcome: outcome("completed"), activity: null };

function message(id: string): TransportMessage {
  return {
    id,
    role: "user",
    turn: 1,
    inherited: false,
    source: null,
    outcome: null,
    parts: [
      { type: "text", text: "看 ", status: "completed" },
      { type: "file", file: `cosir-local-file:${id}-file`, name: "a.md", contentType: "text/markdown", path: `/w/${id}.md` },
    ],
  };
}

describe("moveMessageToComposer", () => {
  it("撤回成功后按快照路径登记附件，并把内容放回主输入框", async () => {
    const canonical = message("m1");
    const thread = toThreadMessage(canonical, context);
    if (thread.role !== "user") throw new Error("expected user message");
    const remove = vi.fn(async () => {});

    await moveMessageToComposer({ taskId: 99201, message: thread, canonical, remove });

    expect(remove).toHaveBeenCalledOnce();
    expect(getLocalAttachmentById("m1-file")).toEqual({ id: "m1-file", name: "a.md", contentType: "text/markdown", path: "/w/m1.md" });
    expect(readComposerDraft(taskDraftKey(99201))?.text).toBe("看 [[cosir-file:m1-file]]");
  });

  it("撤回被拒时原样抛出，不登记附件、不动输入框", async () => {
    const canonical = message("m2");
    const thread = toThreadMessage(canonical, context);
    if (thread.role !== "user") throw new Error("expected user message");

    await expect(moveMessageToComposer({
      taskId: 99202, message: thread, canonical, remove: async () => { throw new Error("409"); },
    })).rejects.toThrow("409");

    expect(getLocalAttachmentById("m2-file")).toBeUndefined();
    expect(readComposerDraft(taskDraftKey(99202))).toBeUndefined();
  });
});
