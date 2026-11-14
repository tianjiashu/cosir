import type { Attachment, CreateAttachment } from "@assistant-ui/core";
import { frontendLog } from "@/lib/logging/frontend-log";
import { extractUserAddMessageAttachments, extractUserAddMessageText, type UserAddMessageCommand } from "../transport-message-converter";
import { editableDraftAttachments, mergeDraftText, type RecoveredDraft, type ApplyEditDraftResult } from "./edit-composer-draft";

/**
 * 输入框内容的进程内记录，比 composer 活得久：composer 属于 runtime，切走 Session 就随 `SessionPage` 销毁。
 * 按 Session 存放（key 即 {@link sessionDraftKey}），草稿与恢复都只针对主输入框；正文自带胶囊标签，
 * 附件只有图片。
 */
export type ComposerDraft = { text: string; attachments: readonly (Attachment | CreateAttachment)[] };
const drafts = new Map<string, ComposerDraft>();
const bindings = new Map<string, (draft: RecoveredDraft) => Promise<ApplyEditDraftResult>>();

export const sessionDraftKey = (sessionId: number): string => `session:${sessionId}`;

export const readComposerDraft = (key: string): ComposerDraft | undefined => drafts.get(key);

/** 切走前由当前 composer 的订阅保存。 */
export function saveComposerDraft(key: string, draft: ComposerDraft): void {
  if (!draft.text && !draft.attachments.length) {
    forgetComposerDraft(key);
    return;
  }
  drafts.set(key, draft);
}

export function forgetComposerDraft(key: string): void {
  drafts.delete(key);
}

/** Session 删除后迟到的恢复不再有去处，它的草稿随之丢弃。 */
export function forgetSessionComposerDrafts(sessionId: number): void {
  forgetComposerDraft(sessionDraftKey(sessionId));
}

/** 绑定只随输入框存活，不随排队消息行存活；旧绑定的清理不删除新绑定。 */
export function bindComposerDraft(key: string, recover: (draft: RecoveredDraft) => Promise<ApplyEditDraftResult>): () => void {
  bindings.set(key, recover);
  return () => { if (bindings.get(key) === recover) bindings.delete(key); };
}

/**
 * 失败/撤回恢复只并入原输入框：还在就同步并入最新内容，已不在就并入保存草稿。
 * DELETE 成功后消息行已卸载不代表恢复失效，不能把已撤回内容丢掉。
 */
export function recoverComposerDraft(key: string, recovered: RecoveredDraft): Promise<ApplyEditDraftResult | null> {
  const recover = bindings.get(key);
  if (recover) return recover(recovered);
  const current = drafts.get(key);
  const attachments = [...(current?.attachments ?? [])];
  const existing = new Set(
    editableDraftAttachments(attachments.map(attachment => ({ ...attachment, content: attachment.content ?? [] })))
      .map((attachment) => attachment.id),
  );
  for (const attachment of editableDraftAttachments(recovered.attachments)) {
    if (!existing.has(attachment.id)) { attachments.push(attachment); existing.add(attachment.id); }
  }
  saveComposerDraft(key, { text: mergeDraftText(recovered.text, current?.text ?? ""), attachments });
  return Promise.resolve(null);
}

/**
 * 被拒命令回到用户手里的唯一入口，只依赖 sessionId：发送的结局不随订阅或页面的生命周期失效，
 * 切走 Session、后端换代后迟到的失败都走这里。门控只能挡界面副作用，不能丢用户内容。返回用户此刻
 * 还在不在这个 Session（主输入框的绑定随 Session 页面存活），没有可恢复的内容时返回 null。
 */
export async function recoverRejectedCommands(sessionId: number, commands: readonly UserAddMessageCommand[]): Promise<"here" | "away" | null> {
  const failed = [...commands].reverse();
  const command = failed.find((item) => extractUserAddMessageText(item).trim() || extractUserAddMessageAttachments(item).length);
  if (!command) return null;
  const draft = { text: extractUserAddMessageText(command), attachments: extractUserAddMessageAttachments(command) };
  if (!draft.text.trim() && !draft.attachments.length) return null;
  const here = bindings.has(sessionDraftKey(sessionId));
  const result = await recoverComposerDraft(sessionDraftKey(sessionId), draft);
  if (result?.error) void frontendLog("ERROR", "assistant_composer_restore_failed", "失败消息恢复附件失败", {
    data: { sessionId, addedCount: result.addedCount, expectedCount: result.expectedCount }, error: result.error,
  });
  return here ? "here" : "away";
}
