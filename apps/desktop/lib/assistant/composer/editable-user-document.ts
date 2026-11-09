import type { CompleteAttachment } from "@assistant-ui/core";

/**
 * 可编辑用户消息的输入框工作文档。
 *
 * 正文（含 `<file>` / `<ref>` / `<skill>` 胶囊标签）由编辑器按胶囊语法解析成原子节点；图片不占正文
 * 位置，单独放在输入框的附件条。本类型只是 UI 投影，canonical 消息仍是唯一事实源。
 */
export type EditableUserDocument = {
  text: string;
  images: readonly CompleteAttachment[];
};

/** 从文本与附件构造输入框工作文档；只保留图片，同一附件只出现一次。 */
export function createEditableUserDocument(
  text: string,
  attachments: readonly CompleteAttachment[],
): EditableUserDocument {
  const images: CompleteAttachment[] = [];
  const seen = new Set<string>();
  for (const attachment of attachments) {
    if (attachment.type !== "image" || seen.has(attachment.id)) continue;
    seen.add(attachment.id);
    images.push(attachment);
  }
  return { text, images };
}

/** 输入框需要恢复的附件：只有图片条。 */
export function editableDocumentAttachments(document: EditableUserDocument): readonly CompleteAttachment[] {
  return document.images;
}
