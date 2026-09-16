import { describe, expect, it, vi } from "vitest";

import {
  applyEditDraftToComposer,
  beginEditComposerOperation,
  currentEditComposerOperation,
  discardPendingEditComposerDraft,
  editableDraftAttachments,
  endEditComposerOperation,
  isCurrentEditComposerOperation,
  isEditComposerOperationActive,
  setPendingEditComposerDraft,
  subscribeEditComposerOperations,
  takePendingEditComposerDraft,
  type EditDraftComposerTarget,
} from "@/lib/assistant/edit-composer-draft";

describe("edit composer coordination", () => {
  it("consumes a pending recovery override only once", () => {
    const messageId = `override-${crypto.randomUUID()}`;
    const draft = { text: "恢复", attachments: [] };
    setPendingEditComposerDraft(messageId, draft);

    expect(takePendingEditComposerDraft(messageId)).toEqual(draft);
    expect(takePendingEditComposerDraft(messageId)).toBeUndefined();
    discardPendingEditComposerDraft(messageId);
  });

  it("keeps a newer recovery generation active when an older one finishes", () => {
    const messageId = `generation-${crypto.randomUUID()}`;
    const listener = vi.fn();
    const unsubscribe = subscribeEditComposerOperations(listener);
    const first = beginEditComposerOperation(messageId);
    const second = beginEditComposerOperation(messageId);

    expect(currentEditComposerOperation(messageId)).toBe(second);
    expect(isCurrentEditComposerOperation(messageId, first)).toBe(false);
    expect(isEditComposerOperationActive(messageId)).toBe(true);

    endEditComposerOperation(messageId, first);
    expect(isEditComposerOperationActive(messageId)).toBe(true);
    endEditComposerOperation(messageId, second);
    expect(isEditComposerOperationActive(messageId)).toBe(false);
    expect(listener).toHaveBeenCalled();
    unsubscribe();
  });
});

type FakeAttachment = {
  id: string;
  type: string;
  name: string;
  content?: readonly { type: string; data?: string }[];
};

/** 与真实 ComposerRuntime 同形的最小 composer 假对象，只保留草稿写入用到的能力。 */
function createComposerStub(initial: readonly FakeAttachment[] = [], failOnAdd = false) {
  let attachments = [...initial];
  const addedIds: string[] = [];
  const removedIds: string[] = [];
  let text = "";
  const composer: EditDraftComposerTarget = {
    getState: () => ({ attachments }),
    addAttachment: async (attachment) => {
      if (failOnAdd) throw new Error("adapter failed");
      addedIds.push(attachment.id ?? "unknown");
      attachments = [
        ...attachments,
        {
          id: attachment.id ?? "unknown",
          type: attachment.type ?? "file",
          name: attachment.name,
          content: attachment.content as FakeAttachment["content"],
        },
      ];
    },
    setText: (next) => {
      text = next;
    },
    attachment: ({ index }) => ({
      remove: async () => {
        removedIds.push(attachments[index]?.id ?? "unknown");
        attachments = attachments.filter((_, position) => position !== index);
      },
    }),
  };
  return {
    composer,
    addedIds,
    removedIds,
    attachments: () => attachments,
    text: () => text,
  };
}

function draftFile(id: string) {
  return {
    id,
    type: "file" as const,
    name: `${id}.md`,
    contentType: "text/markdown",
    content: [
      {
        type: "file" as const,
        data: `cosir-local-file:${id}`,
        filename: `${id}.md`,
        mimeType: "text/markdown",
      },
    ],
  };
}

describe("edit draft composer write", () => {
  it("reuses an attachment already present under the same draft identity", async () => {
    // assistant-ui 的 edit core 会按 message.content lift 出副本：id 不同但 locator 相同。
    const liftCopy: FakeAttachment = {
      id: "lift-copy",
      type: "file",
      name: "keep.md",
      content: [{ type: "file", data: "cosir-local-file:keep" }],
    };
    const stale: FakeAttachment = {
      id: "stale",
      type: "file",
      name: "stale.md",
      content: [{ type: "file", data: "cosir-local-file:stale" }],
    };
    const stub = createComposerStub([liftCopy, stale]);

    const result = await applyEditDraftToComposer({
      composer: stub.composer,
      text: "[[cosir-file:keep]]文字",
      attachments: [draftFile("keep")],
      isStillCurrent: () => true,
    });

    expect(result).toMatchObject({
      expectedCount: 1,
      addedCount: 0,
      removedCount: 1,
      superseded: false,
      error: null,
    });
    expect(stub.addedIds).toEqual([]);
    expect(stub.removedIds).toEqual(["stale"]);
    expect(stub.text()).toBe("[[cosir-file:keep]]文字");
  });

  it("adds missing attachments before writing text", async () => {
    const stub = createComposerStub();

    const result = await applyEditDraftToComposer({
      composer: stub.composer,
      text: "[[cosir-file:a]][[cosir-file:b]]",
      attachments: [draftFile("a"), draftFile("b")],
      isStillCurrent: () => true,
    });

    expect(result).toMatchObject({ addedCount: 2, removedCount: 0, superseded: false, error: null });
    expect(stub.addedIds).toEqual(["a", "b"]);
    expect(stub.text()).toBe("[[cosir-file:a]][[cosir-file:b]]");
  });

  it("leaves text untouched when the draft was superseded", async () => {
    const stub = createComposerStub();

    const result = await applyEditDraftToComposer({
      composer: stub.composer,
      text: "[[cosir-file:a]]",
      attachments: [draftFile("a")],
      isStillCurrent: () => false,
    });

    expect(result.superseded).toBe(true);
    expect(stub.text()).toBe("");
    expect(stub.addedIds).toEqual([]);
  });

  it("degrades to readable text and keeps attachments when a write fails", async () => {
    const stub = createComposerStub([], true);

    const result = await applyEditDraftToComposer({
      composer: stub.composer,
      text: "[[cosir-file:a]]正文",
      attachments: [draftFile("a")],
      isStillCurrent: () => true,
    });

    expect(result.error).toBeInstanceOf(Error);
    expect(stub.text()).toBe("附件正文");
    expect(stub.addedIds).toEqual([]);
  });

  it("drops attachments that have no stable identity", () => {
    expect(editableDraftAttachments([{ name: "无 id", type: "file", contentType: "text/plain", content: [] }])).toEqual([]);
    expect(editableDraftAttachments([draftFile("a")]).map((attachment) => attachment.id)).toEqual(["a"]);
  });
});
