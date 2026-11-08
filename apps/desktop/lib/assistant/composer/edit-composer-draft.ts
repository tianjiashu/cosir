import type { CreateAttachment } from "@assistant-ui/core";

import {
  inlineAttachmentTokenId,
  type InlineAttachmentIdentity,
} from "@/lib/assistant/attachments/local-file-token";

/** 交回输入框的内容：失败恢复与撤回用，都是可重新加入的完整附件。 */
export type RecoveredDraft = {
  text: string;
  attachments: readonly CreateAttachment[];
};

/** 草稿附件必须带稳定身份，否则无法与 composer 里已有附件去重。 */
export type EditDraftAttachment = CreateAttachment & InlineAttachmentIdentity;

/**
 * 草稿写入所需的最小 composer 契约。
 *
 * 只声明「读取草稿、加入完整附件、按索引移除附件、设置文本」四项能力，thread composer 满足它；
 * 单元测试可用同形假对象覆盖，无需 React 运行时。
 */
export type EditDraftComposerTarget = {
  getState(): { text: string; attachments: readonly InlineAttachmentIdentity[] };
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
  /** 为 true 表示输入框已不在，本次没有写入。 */
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
 * 完整草稿只在第一个 await 前提交，异步收尾绝不再写正文或按新附件列表删除。
 * replace 用于初始化（只清理起始快照的旧附件）；prepend 用于失败/撤回恢复（保留当前输入和附件）。
 * AUI 对 CreateAttachment 同步加入完整附件，File 上传另由库管理；这里不接收 File。
 */
export async function applyEditDraftToComposer(options: {
  composer: EditDraftComposerTarget;
  text: string;
  attachments: readonly EditDraftAttachment[];
  mode: "replace" | "prepend";
  isStillCurrent: () => boolean;
}): Promise<ApplyEditDraftResult> {
  const { composer, text, isStillCurrent, mode } = options;
  const images = options.attachments.filter((attachment) => attachment.type === "image");
  const result: ApplyEditDraftResult = { expectedCount: images.length, addedCount: 0, removedCount: 0, superseded: false, error: null };
  if (!isStillCurrent()) return { ...result, superseded: true };
  const pending: Promise<void>[] = [];
  // 单项失败仍提交其它内容；统一收集结果，避免恢复失败后第二次覆盖用户已经继续写的正文。
  const perform = (action: () => Promise<void> | void, completed?: () => void) => {
    try { pending.push(Promise.resolve(action()).then(() => { completed?.(); })); }
    catch (error) { result.error ??= error; }
  };
  const current = composer.getState();
  perform(() => composer.setText(mode === "prepend" ? mergeDraftText(text, current.text) : text));
  const existing = new Set(current.attachments.map(inlineAttachmentTokenId));
  if (mode === "replace") {
    const wanted = new Set(images.map(inlineAttachmentTokenId));
    const kept = new Set<string>();
    const stale = current.attachments.map((attachment, index) => ({ key: inlineAttachmentTokenId(attachment), index }))
      .filter(({ key }) => {
        if (!wanted.has(key) || kept.has(key)) return true;
        kept.add(key);
        return false;
      });
    // 降序按起始列表移除；所有 remove 都在同一同步阶段发起，后续新增附件不在删除范围。
    for (const { index } of stale.reverse()) perform(() => composer.attachment({ index }).remove(), () => result.removedCount++);
  }
  for (const attachment of images) {
    const key = inlineAttachmentTokenId(attachment);
    if (existing.has(key)) continue;
    existing.add(key);
    perform(() => composer.addAttachment(attachment), () => result.addedCount++);
  }
  for (const settled of await Promise.allSettled(pending)) {
    if (settled.status === "rejected") result.error ??= settled.reason;
  }
  return result;
}

/** 恢复的消息在前，用户已经写下的草稿在后；空文本不增加分隔行。 */
export function mergeDraftText(recovered: string, current: string): string {
  return recovered && current.trim() ? `${recovered}\n\n${current}` : recovered || current;
}
