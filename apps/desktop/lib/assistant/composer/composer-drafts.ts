import type { Attachment, CreateAttachment } from "@assistant-ui/core";
import { frontendLog } from "@/lib/logging/frontend-log";
import { inlineAttachmentTokenId } from "../attachments/local-file-token";
import { extractUserAddMessageAttachments, extractUserAddMessageText, getUserAddMessageSourceId, type UserAddMessageCommand } from "../transport-message-converter";
import { editableDraftAttachments, mergeDraftText, type RecoveredDraft, type ApplyEditDraftResult } from "./edit-composer-draft";

/**
 * 输入框内容的进程内记录，比 composer 活得久：composer 属于 runtime，切走 Task 就随 `TaskPage` 销毁。
 * 主输入框与消息编辑框是同一件事，只是 key 不同；编辑框有草稿即表示这条消息处于编辑中。
 */
export type ComposerDraft = { text: string; attachments: readonly (Attachment | CreateAttachment)[] };
const drafts = new Map<string, ComposerDraft>();
const bindings = new Map<string, (draft: RecoveredDraft) => Promise<ApplyEditDraftResult>>();
const listeners = new Set<() => void>();

export const taskDraftKey = (taskId: number): string => `task:${taskId}`;
export const editDraftKey = (taskId: number, messageId: string): string => `edit:${taskId}:${messageId}`;

export const readComposerDraft = (key: string): ComposerDraft | undefined => drafts.get(key);

/** 只在草稿出现或消失时通知：订阅方关心的是「有没有」，每次按键都通知会重渲染所有消息行。 */
export function subscribeComposerDrafts(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** 切走前由当前 composer 的订阅保存。 */
export function saveComposerDraft(key: string, draft: ComposerDraft): void {
  if (!draft.text && !draft.attachments.length) {
    forgetComposerDraft(key);
    return;
  }
  const appeared = !drafts.has(key);
  drafts.set(key, draft);
  if (appeared) for (const listener of listeners) listener();
}

export function forgetComposerDraft(key: string): void {
  if (drafts.delete(key)) for (const listener of listeners) listener();
}

/** Task 删除后迟到的恢复不再有去处，它的草稿随之丢弃。 */
export function forgetTaskComposerDrafts(taskId: number): void {
  const prefix = `edit:${taskId}:`;
  for (const key of [...drafts.keys()]) {
    if (key === taskDraftKey(taskId) || key.startsWith(prefix)) forgetComposerDraft(key);
  }
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
  const existing = new Set(editableDraftAttachments(attachments.map(attachment => ({ ...attachment, content: attachment.content ?? [] }))).map(inlineAttachmentTokenId));
  for (const attachment of editableDraftAttachments(recovered.attachments)) {
    const id = inlineAttachmentTokenId(attachment);
    if (!existing.has(id)) { attachments.push(attachment); existing.add(id); }
  }
  saveComposerDraft(key, { text: mergeDraftText(recovered.text, current?.text ?? ""), attachments });
  return Promise.resolve(null);
}

/**
 * 被拒命令回到用户手里的唯一入口，只依赖 taskId：发送的结局不随订阅或页面的生命周期失效，
 * 切走 Task、后端换代后迟到的失败都走这里。门控只能挡界面副作用，不能丢用户内容。
 * 编辑命令回到它的编辑草稿，消息行据此回到编辑态。返回用户此刻还在不在这个 Task（主输入框的
 * 绑定随 Task 页面存活），没有可恢复的内容时返回 null。
 */
export async function recoverRejectedCommands(taskId: number, commands: readonly UserAddMessageCommand[]): Promise<"here" | "away" | null> {
  const failed = [...commands].reverse();
  const command = failed.find((item) => getUserAddMessageSourceId(item) !== null)
    ?? failed.find((item) => extractUserAddMessageText(item).trim() || extractUserAddMessageAttachments(item).length);
  if (!command) return null;
  const draft = { text: extractUserAddMessageText(command), attachments: extractUserAddMessageAttachments(command) };
  if (!draft.text.trim() && !draft.attachments.length) return null;
  const here = bindings.has(taskDraftKey(taskId));
  const sourceId = getUserAddMessageSourceId(command);
  const result = await recoverComposerDraft(sourceId === null ? taskDraftKey(taskId) : editDraftKey(taskId, sourceId), draft);
  if (result?.error) void frontendLog("ERROR", "assistant_composer_restore_failed", "失败消息恢复附件失败", {
    data: { taskId, addedCount: result.addedCount, expectedCount: result.expectedCount }, error: result.error,
  });
  return here ? "here" : "away";
}
