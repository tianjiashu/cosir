import type { ThreadUserMessage } from "@assistant-ui/react";

import { recoverComposerDraft, taskDraftKey } from "@/lib/assistant/composer/composer-drafts";
import { editableDocumentAttachments } from "@/lib/assistant/composer/editable-user-document";
import type { TransportMessage } from "@/lib/assistant/contract";
import { toEditableUserMessageDraft } from "@/lib/assistant/transport-message-converter";
import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 把一条用户消息放回主输入框，排在已有内容之前：撤回已并入的消息与编辑排队消息共用这一条路。
 *
 * 编辑不是后端命令：这里先执行 `remove`（撤回回合或删除排队消息），成功后才放回，改完照常发送。
 * `remove` 失败（已被并入、已不是最新一条、仍在运行）时原样抛出，输入框不动。
 *
 * 正文里的 `<file>` / `<ref>` / `<skill>` 标签就是胶囊指针，随文本原样回填，编辑器按胶囊语法重新
 * 解析成原子节点；只有图片作为附件恢复。用户已切走时内容并入保存的草稿（见 `recoverComposerDraft`）。
 */
export async function moveMessageToComposer(options: {
  taskId: number;
  message: ThreadUserMessage;
  canonical: Pick<TransportMessage, "parts">;
  remove: () => Promise<void>;
}): Promise<void> {
  const { taskId, message, remove } = options;
  // 内容在 remove 之前取出：成功后消息随快照消失，消息作用域也跟着卸载。
  const draft = toEditableUserMessageDraft(message);
  await remove();
  const result = await recoverComposerDraft(taskDraftKey(taskId), {
    text: draft.text,
    attachments: editableDocumentAttachments(draft.document),
  });
  if (result?.error) {
    void frontendLog("ERROR", "message_composer_restore_failed", "消息放回输入框时附件恢复失败", {
      data: { taskId, messageId: message.id, addedCount: result.addedCount, expectedCount: result.expectedCount },
      error: result.error,
    });
  }
}
