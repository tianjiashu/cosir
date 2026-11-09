import { describe, expect, it } from "vitest";

import {
  applyEditDraftToComposer,
  editableDraftAttachments,
  type EditDraftComposerTarget,
} from "@/lib/assistant/composer/edit-composer-draft";

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
    getState: () => ({ text, attachments }),
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

function draftImage(id: string) {
  return { id, type: "image" as const, name: `${id}.png`, contentType: "image/png",
    content: [{ type: "image" as const, image: `cosir-attachment://${id}` }] };
}

describe("edit draft composer write", () => {
  it("清理起始快照之外附件，只把图片加入附件条，正文胶囊留在文本里", async () => {
    const liftCopy: FakeAttachment = { id: "lift-copy", type: "file", name: "keep.md" };
    const stale: FakeAttachment = { id: "stale", type: "image", name: "stale.png" };
    const stub = createComposerStub([liftCopy, stale]);

    const result = await applyEditDraftToComposer({
      mode: "replace",
      composer: stub.composer,
      text: '<file path="/tmp/keep.md" />文字',
      attachments: [draftImage("keep")],
      isStillCurrent: () => true,
    });

    expect(result).toMatchObject({
      expectedCount: 1,
      addedCount: 1,
      removedCount: 2,
      superseded: false,
      error: null,
    });
    expect(stub.addedIds).toEqual(["keep"]);
    expect(stub.removedIds).toEqual(["stale", "lift-copy"]);
    expect(stub.text()).toBe('<file path="/tmp/keep.md" />文字');
  });

  it("已存在的同 id 图片不重复加入", async () => {
    const stub = createComposerStub([draftImage("a")]);

    const result = await applyEditDraftToComposer({
      mode: "replace",
      composer: stub.composer,
      text: "正文",
      attachments: [draftImage("a")],
      isStillCurrent: () => true,
    });

    expect(result).toMatchObject({ addedCount: 0, removedCount: 0, superseded: false, error: null });
    expect(stub.addedIds).toEqual([]);
  });

  it("leaves text untouched when the draft was superseded", async () => {
    const stub = createComposerStub();

    const result = await applyEditDraftToComposer({
      mode: "replace",
      composer: stub.composer,
      text: "正文",
      attachments: [draftImage("a")],
      isStillCurrent: () => false,
    });

    expect(result.superseded).toBe(true);
    expect(stub.text()).toBe("");
    expect(stub.addedIds).toEqual([]);
  });

  it("图片恢复失败时仍保留正文", async () => {
    const stub = createComposerStub([], true);

    const result = await applyEditDraftToComposer({
      mode: "replace",
      composer: stub.composer,
      text: '正文 <file path="/tmp/a.md" /> `示例`',
      attachments: [draftImage("fail")],
      isStillCurrent: () => true,
    });

    expect(result.error).toBeInstanceOf(Error);
    expect(stub.text()).toBe('正文 <file path="/tmp/a.md" /> `示例`');
    expect(stub.addedIds).toEqual([]);
  });

  it("drops attachments that are not images or have no stable identity", () => {
    expect(editableDraftAttachments([
      { name: "无 id", type: "image", contentType: "image/png", content: [] },
      { id: "file-a", name: "a.md", type: "file", contentType: "text/markdown", content: [] },
    ])).toEqual([]);
    expect(editableDraftAttachments([draftImage("a")]).map((attachment) => attachment.id)).toEqual(["a"]);
  });
});

describe("draft recovery commit boundary", () => {
  it("prepends recovered content while preserving the current draft and attachments", async () => {
    const stub = createComposerStub([draftImage("current")]);
    stub.composer.setText("new input");
    await applyEditDraftToComposer({ composer: stub.composer, mode: "prepend", text: "failed message", attachments: [draftImage("old")], isStillCurrent: () => true });
    expect(stub.text()).toBe("failed message\n\nnew input");
    expect(stub.attachments().map(a => a.id)).toEqual(["current", "old"]);
    expect(stub.removedIds).toEqual([]);
  });

  for (const mode of ["replace", "prepend"] as const) it(`${mode}: attachment completion never rewrites later user input or removes new images`, async () => {
    const stub = createComposerStub();
    let finish!: () => void;
    const gate = new Promise<void>(resolve => { finish = resolve; });
    const add = stub.composer.addAttachment;
    stub.composer.addAttachment = async attachment => {
      await add(attachment); // complete attachments mutate synchronously, as AUI does
      await gate;
    };
    const applying = applyEditDraftToComposer({ composer: stub.composer, mode, text: "restored", attachments: [draftImage("old")], isStillCurrent: () => true });
    expect(stub.text()).toBe("restored");
    stub.composer.setText("restored and newly typed");
    await add(draftImage("new"));
    finish();
    await applying;
    expect(stub.text()).toBe("restored and newly typed");
    expect(stub.attachments().map(a => a.id)).toEqual(["old", "new"]);
  });
});
