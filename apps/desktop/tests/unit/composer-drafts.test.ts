import { describe, expect, it, vi } from "vitest";
import { bindComposerDraft, forgetTaskComposerDrafts, readComposerDraft, recoverComposerDraft, recoverRejectedCommands, saveComposerDraft, taskDraftKey } from "@/lib/assistant/composer/composer-drafts";
import { applyEditDraftToComposer, editableDraftAttachments } from "@/lib/assistant/composer/edit-composer-draft";
import type { UserAddMessageCommand } from "@/lib/assistant/transport-message-converter";

const draft = (text: string) => ({ text, attachments: [] });

describe("Task draft ownership", () => {
  it("keeps a late withdrawn message in its original Task after switching away", async () => {
    saveComposerDraft("task:99001", draft("original draft"));
    saveComposerDraft("task:99002", draft("other task"));
    await recoverComposerDraft(taskDraftKey(99001), draft("withdrawn"));
    expect(readComposerDraft("task:99001")?.text).toBe("withdrawn\n\noriginal draft");
    expect(readComposerDraft("task:99002")?.text).toBe("other task");
  });

  it("concurrent withdrawals read the live draft and never overwrite one another", async () => {
    let text = "typed";
    const composer = { getState: () => ({ text, attachments: [] }), setText: (value: string) => { text = value; }, addAttachment: async () => {}, attachment: () => ({ remove: () => {} }) };
    const unbind = bindComposerDraft("task:99003", recovered => applyEditDraftToComposer({ composer, ...recovered, attachments: editableDraftAttachments(recovered.attachments), mode: "prepend", isStillCurrent: () => true }));
    await Promise.all([recoverComposerDraft(taskDraftKey(99003), draft("first response")), recoverComposerDraft(taskDraftKey(99003), draft("second response"))]);
    expect(text).toBe("second response\n\nfirst response\n\ntyped");
    unbind();
  });

  it("old binding cleanup cannot detach the current composer", async () => {
    const stale = bindComposerDraft("task:99004", vi.fn());
    const current = vi.fn(async () => ({ expectedCount: 0, addedCount: 0, removedCount: 0, superseded: false, error: null }));
    const cleanup = bindComposerDraft("task:99004", current);
    stale();
    await recoverComposerDraft(taskDraftKey(99004), draft("recovered"));
    expect(current).toHaveBeenCalledOnce();
    cleanup();
  });
});

const rejected = (text: string): UserAddMessageCommand => ({ type: "add-message", message: { role: "user", parts: [{ type: "text", text }] } });

describe("recoverRejectedCommands", () => {
  it("returns a rejected send to the Task's saved draft after the user left", async () => {
    saveComposerDraft("task:99101", draft("written later"));
    await expect(recoverRejectedCommands(99101, [rejected("long message")])).resolves.toBe("away");
    expect(readComposerDraft("task:99101")?.text).toBe("long message\n\nwritten later");
  });

  it("reports that the user is still on the Task", async () => {
    const recover = vi.fn(async () => ({ expectedCount: 0, addedCount: 0, removedCount: 0, superseded: false, error: null }));
    const unbind = bindComposerDraft("task:99102", recover);
    await expect(recoverRejectedCommands(99102, [rejected("message")])).resolves.toBe("here");
    expect(recover).toHaveBeenCalledOnce();
    unbind();
  });

  it("ignores a command with nothing to give back", async () => {
    await expect(recoverRejectedCommands(99104, [rejected("   ")])).resolves.toBeNull();
    expect(readComposerDraft("task:99104")).toBeUndefined();
  });
});

describe("draft lifetime", () => {
  it("deleting a Task drops its input draft, not other Tasks' with a similar id", () => {
    saveComposerDraft(taskDraftKey(99105), draft("main"));
    saveComposerDraft(taskDraftKey(991050), draft("other"));
    forgetTaskComposerDrafts(99105);
    expect(readComposerDraft(taskDraftKey(99105))).toBeUndefined();
    expect(readComposerDraft(taskDraftKey(991050))?.text).toBe("other");
    forgetTaskComposerDrafts(991050);
  });
});
