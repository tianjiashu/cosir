import { describe, expect, it, vi } from "vitest";
import { getLocalAttachmentById, registerLocalAttachment } from "@/lib/assistant/attachments/local-attachment-registry";

import {
  applyEditDraftToComposer,
  beginEditComposerOperation,
  consumePendingEditComposerStart,
  currentEditComposerOperation,
  discardPendingEditComposerDraft,
  editableDraftAttachments,
  endEditComposerOperation,
  hasPendingEditComposerStart,
  isCurrentEditComposerOperation,
  isEditComposerOperationActive,
  isQueuedEditLocked,
  lockQueuedEditMode,
  queuePendingEditComposerStart,
  releaseQueuedEditLock,
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

  it("queues the message-scoped edit action as a one-shot request", () => {
    const messageId = `edit-start-${crypto.randomUUID()}`;
    queuePendingEditComposerStart(messageId);

    expect(hasPendingEditComposerStart(messageId)).toBe(true);
    expect(consumePendingEditComposerStart(messageId)).toBe(true);
    expect(consumePendingEditComposerStart(messageId)).toBe(false);
    expect(hasPendingEditComposerStart(messageId)).toBe(false);
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

function draftImage(id: string) {
  return { id, type: "image" as const, name: `${id}.png`, contentType: "image/png",
    content: [{ type: "image" as const, image: `cosir-attachment://${id}` }] };
}

describe("queued edit mode lock", () => {
  it("keeps the queued mode for a restored draft after the message was consumed", () => {
    const messageId = `queued-${crypto.randomUUID()}`;
    expect(lockQueuedEditMode(messageId, true, false)).toBe(true);

    // 提交被拒（消息已被并入）后恢复编辑框：此时已不在排队，仍按排队编辑处理，不会改成重跑。
    expect(lockQueuedEditMode(messageId, false, true)).toBe(true);
    expect(isQueuedEditLocked(messageId)).toBe(true);
  });

  it("re-decides the mode when a fresh edit starts", () => {
    const messageId = `queued-${crypto.randomUUID()}`;
    lockQueuedEditMode(messageId, true, false);

    expect(lockQueuedEditMode(messageId, false, false)).toBe(false);
    expect(isQueuedEditLocked(messageId)).toBe(false);
  });

  it("releases the lock after a save or an explicit switch to rerun", () => {
    const messageId = `queued-${crypto.randomUUID()}`;
    lockQueuedEditMode(messageId, true, false);

    releaseQueuedEditLock(messageId);

    expect(isQueuedEditLocked(messageId)).toBe(false);
    expect(lockQueuedEditMode(messageId, false, true)).toBe(false);
  });
});

describe("edit draft composer write", () => {
  it("将历史普通文件登记为元数据并清除 AUI lift 副本", async () => {
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
      expectedCount: 0,
      addedCount: 0,
      removedCount: 2,
      superseded: false,
      error: null,
    });
    expect(stub.addedIds).toEqual([]);
    expect(stub.removedIds).toEqual(["stale", "lift-copy"]);
    expect(getLocalAttachmentById("keep")).toMatchObject({ name: "keep.md", kind: "file" });
    expect(stub.text()).toBe("[[cosir-file:keep]]文字");
  });

  it("只恢复图片集合，普通文件通过 token 恢复", async () => {
    const stub = createComposerStub();

    const result = await applyEditDraftToComposer({
      composer: stub.composer,
      text: "[[cosir-file:a]][[cosir-file:b]]",
      attachments: [draftFile("a"), draftImage("b")],
      isStillCurrent: () => true,
    });

    expect(result).toMatchObject({ addedCount: 1, removedCount: 0, superseded: false, error: null });
    expect(stub.addedIds).toEqual(["b"]);
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

  it("图片恢复失败时仍保留普通文件 token 与正文", async () => {
    const stub = createComposerStub([], true);

    const result = await applyEditDraftToComposer({
      composer: stub.composer,
      text: "[[cosir-file:a]]正文 `[[cosir-file:example]]`",
      attachments: [draftFile("a"), draftImage("fail")],
      isStillCurrent: () => true,
    });

    expect(result.error).toBeInstanceOf(Error);
    expect(stub.text()).toBe("[[cosir-file:a]]正文 `[[cosir-file:example]]`");
    expect(stub.addedIds).toEqual([]);
  });

  it("历史展示元数据不会覆盖撤销所需的真实本机路径", async () => {
    registerLocalAttachment(new File([], "native.md"), {
      id: "native", name: "native.md", path: "/workspace/native.md", contentType: "text/plain", kind: "file",
    });
    const stub = createComposerStub();
    await applyEditDraftToComposer({ composer: stub.composer, text: "[[cosir-file:native]]", attachments: [draftFile("native")], isStillCurrent: () => true });
    expect(getLocalAttachmentById("native")?.path).toBe("/workspace/native.md");
    expect(stub.attachments()).toEqual([]);
  });

  it("drops attachments that have no stable identity", () => {
    expect(editableDraftAttachments([{ name: "无 id", type: "file", contentType: "text/plain", content: [] }])).toEqual([]);
    expect(editableDraftAttachments([draftFile("a")]).map((attachment) => attachment.id)).toEqual(["a"]);
  });
});
