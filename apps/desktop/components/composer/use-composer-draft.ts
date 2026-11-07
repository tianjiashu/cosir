import { useContext, useEffect, useRef, useState } from "react";
import { useAui, useAuiState } from "@assistant-ui/react";
import { toast } from "sonner";
import { ReferenceTargetScope } from "@/components/workspace-references/reference-bridge";
import {
  bindComposerDraft,
  readComposerDraft,
  saveComposerDraft,
  type ComposerDraft,
} from "@/lib/assistant/composer-drafts";
import {
  applyEditDraftToComposer,
  editableDraftAttachments,
} from "@/lib/assistant/edit-composer-draft";
import { frontendLog } from "@/lib/logging/frontend-log";

/** 消息编辑框的进入方式。`seed` 是没有草稿时的内容；`continued` 表示 composer 还活着，内容不用恢复。 */
export type EditDraftEntry = { seed: ComposerDraft | null; continued: boolean };

/**
 * 让 composer 的内容比 runtime 活得久：在 AUI 仍存活时保存正文与图片，挂载时恢复，并接住失败恢复。
 * 普通文件引用随正文保存，展示元数据复用登记表。返回是否正在恢复（此时不能提交）。
 *
 * 主输入框只在自己为空时恢复；编辑框的 composer 一进来就带着原消息和 AUI 提取的附件，所以总是替换。
 */
export function useComposerDraft(key: string | null, edit?: EditDraftEntry): boolean {
  const aui = useAui();
  const text = useAuiState((state) => state.composer.text);
  const attachments = useAuiState((state) => state.composer.attachments);
  const ready = useRef(false);
  const entry = useRef(edit);
  const isEdit = edit !== undefined;
  const [restoring, setRestoring] = useState(() => key !== null && (edit ? !edit.continued : Boolean(readComposerDraft(key))));
  useEffect(() => {
    if (!key) return;
    let active = true;
    ready.current = false;
    // 编辑框不绑定：提交已结束这次编辑，失败内容进草稿，消息行据此重新进入编辑。
    const unbind = entry.current ? undefined : bindComposerDraft(key, (draft) => {
      const result = applyEditDraftToComposer({ composer: aui.composer, text: draft.text,
        attachments: editableDraftAttachments(draft.attachments), mode: "prepend", isStillCurrent: () => active });
      // 写入已同步完成；保存最新值，不等 React 下次 effect 才记下已撤回内容。
      const state = aui.composer.getState();
      saveComposerDraft(key, { text: state.text, attachments: [...state.attachments] });
      return result;
    });
    const saved = readComposerDraft(key) ?? entry.current?.seed;
    const current = aui.composer.getState();
    void frontendLog("DEBUG", "composer_draft_restoring", "恢复前端草稿", {
      data: {
        key,
        savedLength: saved?.text.length,
        currentLength: current.text.length,
      },
    });
    // StrictMode 可在加入附件后重启 effect；由共享恢复器去重，仍需恢复正文。
    if (saved && (entry.current ? !entry.current.continued : !current.text)) {
      setRestoring(true);
      void (async () => {
        try {
          const completed = saved.attachments.filter(
            (attachment) => !("status" in attachment) || attachment.status.type === "complete",
          );
          const result = await applyEditDraftToComposer({
            mode: "replace",
            composer: aui.composer,
            text: saved.text,
            attachments: editableDraftAttachments(
              completed.map((attachment) => ({
                ...attachment,
                content: attachment.content ?? [],
              })),
            ),
            isStillCurrent: () => active,
          });
          if (result.error) throw result.error;
          // 尚未上传的图片仍由原附件适配器处理，不能伪装成已经完成的附件。
          for (const attachment of saved.attachments) {
            if (!active) return;
            if ("status" in attachment && attachment.status.type !== "complete" && attachment.file)
              await aui.composer.addAttachment(attachment.file);
          }
        } catch (error) {
          if (active) {
            toast.error("部分草稿附件恢复失败，请重新添加");
            void frontendLog(
              "WARNING",
              "composer_draft_restore_failed",
              "草稿附件恢复失败",
              { error, data: { key } },
            );
          }
        } finally {
          if (active) {
            ready.current = true;
            setRestoring(false);
          }
        }
      })();
    } else {
      ready.current = true;
      setRestoring(false);
    }
    return () => {
      active = false;
      unbind?.();
    };
  }, [aui, key]);
  // 卸载阶段 AUI 可能已释放 runtime，不能等 cleanup 再读取草稿。
  useEffect(() => {
    if (!key || !ready.current || restoring) return;
    // 编辑框清空正文不等于放弃编辑：草稿的有无表示「在编辑中」，只由取消和提交结束。
    if (isEdit && !text) return;
    saveComposerDraft(key, { text, attachments: [...attachments] });
  }, [key, text, attachments, restoring, isEdit]);
  return restoring;
}

/** 主输入框：草稿 key 就是它所在的 Task。 */
export function useTaskComposerDraft(enabled: boolean): boolean {
  const target = useContext(ReferenceTargetScope);
  return useComposerDraft(enabled && target?.startsWith("task:") ? target : null);
}
