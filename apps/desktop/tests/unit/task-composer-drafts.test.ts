import { describe, expect, it, vi } from "vitest";
import { bindTaskComposerDraft, readTaskComposerDraft, recoverTaskComposerDraft, saveTaskComposerDraft } from "@/lib/assistant/task-composer-drafts";
import { applyEditDraftToComposer, editableDraftAttachments } from "@/lib/assistant/edit-composer-draft";

const draft = (text: string) => ({ text, attachments: [] });

describe("Task draft ownership", () => {
  it("keeps a late withdrawn message in its original Task after switching away", async () => {
    saveTaskComposerDraft("task:99001", draft("original draft"));
    saveTaskComposerDraft("task:99002", draft("other task"));
    await recoverTaskComposerDraft(99001, draft("withdrawn"));
    expect(readTaskComposerDraft("task:99001")?.text).toBe("withdrawn\n\noriginal draft");
    expect(readTaskComposerDraft("task:99002")?.text).toBe("other task");
  });

  it("concurrent withdrawals read the live draft and never overwrite one another", async () => {
    let text = "typed";
    const composer = { getState: () => ({ text, attachments: [] }), setText: (value: string) => { text = value; }, addAttachment: async () => {}, attachment: () => ({ remove: () => {} }) };
    const unbind = bindTaskComposerDraft("task:99003", recovered => applyEditDraftToComposer({ composer, ...recovered, attachments: editableDraftAttachments(recovered.attachments), mode: "prepend", isStillCurrent: () => true }));
    await Promise.all([recoverTaskComposerDraft(99003, draft("first response")), recoverTaskComposerDraft(99003, draft("second response"))]);
    expect(text).toBe("second response\n\nfirst response\n\ntyped");
    unbind();
  });

  it("old binding cleanup cannot detach the current composer", async () => {
    const stale = bindTaskComposerDraft("task:99004", vi.fn());
    const current = vi.fn(async () => ({ expectedCount: 0, addedCount: 0, removedCount: 0, superseded: false, error: null }));
    const cleanup = bindTaskComposerDraft("task:99004", current);
    stale();
    await recoverTaskComposerDraft(99004, draft("recovered"));
    expect(current).toHaveBeenCalledOnce();
    cleanup();
  });
});
