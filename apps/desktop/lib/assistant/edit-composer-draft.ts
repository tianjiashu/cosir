import type { CreateAttachment } from "@assistant-ui/core";

import {
  inlineAttachmentTokenId,
  LOCAL_FILE_TOKEN,
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
  /** 草稿声明的附件数。 */
  expectedCount: number;
  /** 实际新加入 composer 的附件数（同键附件不重复加入）。 */
  addedCount: number;
  /** 清理掉的「草稿之外」附件数。 */
  removedCount: number;
  /** 为 true 表示写入途中草稿已被新代际取代，文本未被改写。 */
  superseded: boolean;
  /** 写入异常；非 null 时文本已降级为不含内部 token 的可读形式。 */
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
 * 把一份编辑草稿原子地写入 composer。
 *
 * 负责什么：按「加入缺失附件 → 写入文本 → 清理草稿之外的附件」的顺序收敛 composer 状态，
 * 并保证任一中断点都不会留下「文本含 token 但附件缺失」的中间态——那会让内联 token 渲染
 * 成「附件已失效」。
 * 不负责什么：不计算草稿内容（由 `toEditableUserMessageDraft` 负责）、不做代际或生命周期
 * 决策（由调用方通过 `isStillCurrent` 提供）、不写日志（由调用方基于返回值记录）。
 *
 * 参数:
 *     composer: 目标 composer；只使用上述四项能力。
 *     text: 要写入的文本，可含 `[[cosir-file:id]]` 内部 token。
 *     attachments: 草稿声明的附件，按顺序加入；同键（`inlineAttachmentTokenId`）已存在时跳过。
 *     isStillCurrent: 返回 false 时立即停止后续步骤并保留当前状态，用于编辑代际被取代。
 *
 * 返回:
 *     写入统计与错误；`superseded` 为 true 时文本未被改写。
 *
 * 异常/副作用:
 *     不抛出：composer 或附件适配器的异常会被捕获，经 `error` 返回，并把文本降级为可读形式。
 *     副作用：修改 composer 的附件列表与文本。
 */
export async function applyEditDraftToComposer(options: {
  composer: EditDraftComposerTarget;
  text: string;
  attachments: readonly EditDraftAttachment[];
  isStillCurrent: () => boolean;
}): Promise<ApplyEditDraftResult> {
  const { composer, text, attachments, isStillCurrent } = options;
  const expectedCount = attachments.length;
  let addedCount = 0;
  let removedCount = 0;

  try {
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
    // 写入失败时保留可读文本：不把内部 token 留在输入框里，避免出现无法操作的占位符。
    composer.setText(text.replace(LOCAL_FILE_TOKEN, "附件"));
    return { expectedCount, addedCount, removedCount, superseded: false, error };
  }
}
