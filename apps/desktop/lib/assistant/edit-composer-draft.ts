import type { CreateAttachment } from "@assistant-ui/core";
import { registerInlineFileMetadata } from "@/lib/assistant/attachments/local-attachment-registry";

import {
  inlineAttachmentTokenId,
  type InlineAttachmentIdentity,
} from "@/lib/assistant/attachments/local-file-token";

export type EditComposerDraftOverride = {
  text: string;
  attachments: readonly CreateAttachment[];
};

// The edit composer is created by assistant-ui immediately after beginEdit.
// Keep a one-shot override outside React so failure recovery can hand the
// exact transport draft to the newly mounted edit view without racing its
// canonical-message hydration effect. The message ID is used instead of a
// runtime object because assistant-ui may expose different accessor wrappers
// for the same composer.
const pendingOverrides = new Map<string, EditComposerDraftOverride>();
const activeGenerations = new Map<string, number>();
const pendingEditStarts = new Set<string>();
const activeListeners = new Set<() => void>();

function notifyActiveListeners(): void {
  for (const listener of activeListeners) listener();
}

export function setPendingEditComposerDraft(
  messageId: string,
  draft: EditComposerDraftOverride,
): void {
  pendingOverrides.set(messageId, draft);
}

export function takePendingEditComposerDraft(
  messageId: string,
): EditComposerDraftOverride | undefined {
  const draft = pendingOverrides.get(messageId);
  if (draft) pendingOverrides.delete(messageId);
  return draft;
}

export function discardPendingEditComposerDraft(messageId: string): void {
  pendingOverrides.delete(messageId);
  pendingEditStarts.delete(messageId);
  notifyActiveListeners();
}

/** 请求消息行在自身 AUI 上下文中开始编辑，避免从 Thread 上下文创建另一份 composer。 */
export function queuePendingEditComposerStart(messageId: string): void {
  pendingEditStarts.add(messageId);
  notifyActiveListeners();
}

export function hasPendingEditComposerStart(messageId: string): boolean {
  return pendingEditStarts.has(messageId);
}

export function consumePendingEditComposerStart(messageId: string): boolean {
  const consumed = pendingEditStarts.delete(messageId);
  if (consumed) notifyActiveListeners();
  return consumed;
}

export function beginEditComposerOperation(messageId: string): number {
  const generation = (activeGenerations.get(messageId) ?? 0) + 1;
  activeGenerations.set(messageId, generation);
  notifyActiveListeners();
  return generation;
}

export function isCurrentEditComposerOperation(messageId: string, generation: number): boolean {
  return activeGenerations.get(messageId) === generation;
}

export function currentEditComposerOperation(messageId: string): number | undefined {
  return activeGenerations.get(messageId);
}

export function endEditComposerOperation(messageId: string, generation: number): void {
  if (isCurrentEditComposerOperation(messageId, generation)) {
    activeGenerations.delete(messageId);
    notifyActiveListeners();
  }
}

export function isEditComposerOperationActive(messageId: string): boolean {
  return activeGenerations.has(messageId);
}

export function subscribeEditComposerOperations(listener: () => void): () => void {
  activeListeners.add(listener);
  return () => activeListeners.delete(listener);
}

/** 草稿附件必须带稳定身份，否则无法与 composer 里已有附件去重。 */
export type EditDraftAttachment = CreateAttachment & InlineAttachmentIdentity;

/**
 * 草稿写入所需的最小 composer 契约。
 *
 * 只声明「读取附件、加入附件、按索引移除附件、设置文本」四项能力，因此 thread composer
 * 与 message edit composer 都能满足；单元测试可用同形假对象覆盖，无需 React 运行时。
 */
export type EditDraftComposerTarget = {
  getState(): { attachments: readonly InlineAttachmentIdentity[] };
  addAttachment(attachment: CreateAttachment): Promise<void>;
  setText(text: string): void;
  attachment(options: { index: number }): { remove(): Promise<void> | void };
};

/** 一次草稿写入的结果，供调用方记录日志并判断写入是否完整。 */
export type ApplyEditDraftResult = {
  /** 需要恢复到 AUI 的图片数。 */
  expectedCount: number;
  /** 实际新加入 composer 的附件数（同键附件不重复加入）。 */
  addedCount: number;
  /** 清理掉的「草稿之外」附件数。 */
  removedCount: number;
  /** 为 true 表示写入途中草稿已被新代际取代，文本未被改写。 */
  superseded: boolean;
  /** 写入异常；非 null 时保留正文，由调用方报告图片恢复失败。 */
  error: unknown;
};

/**
 * 收窄为具备稳定身份的草稿附件。
 *
 * 负责什么：只保留带非空 `id` 的附件——没有 id 的附件无法与内联 token 关联，也无法与
 * composer 里已有附件去重。
 * 不负责什么：不补 id、不做去重（去重由 `applyEditDraftToComposer` 按 `inlineAttachmentTokenId` 完成）。
 *
 * 参数:
 *     attachments: 任意来源（canonical 投影或失败恢复）的待写入附件。
 *
 * 返回:
 *     具备稳定 id 的附件子集，保持原顺序。
 *
 * 异常/副作用:
 *     无；纯函数。
 */
export function editableDraftAttachments(
  attachments: readonly CreateAttachment[],
): readonly EditDraftAttachment[] {
  return attachments.filter(
    (attachment): attachment is EditDraftAttachment =>
      typeof attachment.id === "string" && attachment.id.length > 0,
  );
}

/**
 * 普通文件只登记元数据，当前引用集合由文本 token 决定；AUI 仅恢复图片附件。
 * 先登记文件、补齐图片，再写文本并移除 AUI 自动从历史消息提取的多余附件。
 * 这是可中断的收敛过程，不是事务；失效操作不再写草稿，失败保留正文并返回错误。
 */
export async function applyEditDraftToComposer(options: {
  composer: EditDraftComposerTarget;
  text: string;
  attachments: readonly EditDraftAttachment[];
  isStillCurrent: () => boolean;
}): Promise<ApplyEditDraftResult> {
  const { composer, text, isStillCurrent } = options;
  const attachments = options.attachments.filter((attachment) => attachment.type === "image");
  const expectedCount = attachments.length;
  let addedCount = 0;
  let removedCount = 0;

  try {
    if (!isStillCurrent()) return { expectedCount, addedCount, removedCount, superseded: true, error: null };
    for (const attachment of options.attachments) {
      if (attachment.type !== "file") continue;
      registerInlineFileMetadata({
        id: inlineAttachmentTokenId(attachment),
        name: attachment.name,
        contentType: attachment.contentType ?? "application/octet-stream",
      });
    }
    const existingKeys = new Set(composer.getState().attachments.map(inlineAttachmentTokenId));
    for (const attachment of attachments) {
      if (!isStillCurrent()) {
        return { expectedCount, addedCount, removedCount, superseded: true, error: null };
      }
      const key = inlineAttachmentTokenId(attachment);
      if (existingKeys.has(key)) continue;
      await composer.addAttachment(attachment);
      existingKeys.add(key);
      addedCount += 1;
    }

    if (!isStillCurrent()) {
      return { expectedCount, addedCount, removedCount, superseded: true, error: null };
    }
    composer.setText(text);

    // 清理两类多余附件：草稿之外的附件（例如 assistant-ui 默认 edit core 在挂载时按
    // message.content lift 出来的副本），以及同一草稿身份的多余副本（只保留第一个）。
    // 放在最后且失败无害：即使中断也只是多留附件，不会缺附件。
    const draftKeys = new Set(attachments.map(inlineAttachmentTokenId));
    const keptKeys = new Set<string>();
    const staleIndexes = composer.getState().attachments
      .map((attachment, index) => ({ key: inlineAttachmentTokenId(attachment), index }))
      .filter((entry) => {
        if (!draftKeys.has(entry.key)) return true;
        if (keptKeys.has(entry.key)) return true;
        keptKeys.add(entry.key);
        return false;
      })
      .map((entry) => entry.index)
      .reverse();
    for (const index of staleIndexes) {
      if (!isStillCurrent()) break;
      await composer.attachment({ index }).remove();
      removedCount += 1;
    }

    return { expectedCount, addedCount, removedCount, superseded: false, error: null };
  } catch (error) {
    if (isStillCurrent()) composer.setText(text);
    return { expectedCount, addedCount, removedCount, superseded: false, error };
  }
}
