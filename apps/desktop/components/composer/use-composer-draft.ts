import { useContext, useEffect, useRef, useState } from "react";
import { useAui, useAuiState } from "@assistant-ui/react";
import { toast } from "sonner";
import { ReferenceTargetScope } from "@/components/workspace-references/reference-bridge";
import {
  bindComposerDraft,
  readComposerDraft,
  saveComposerDraft,
} from "@/lib/assistant/composer/composer-drafts";
import {
  applyEditDraftToComposer,
  editableDraftAttachments,
} from "@/lib/assistant/composer/edit-composer-draft";
import { frontendLog } from "@/lib/logging/frontend-log";

/**
 * 让主输入框的内容比 runtime 活得久：在 AUI 仍存活时保存正文与图片，挂载时恢复，并接住失败与撤回
 * 恢复。普通文件、引用与 Skill 都只是正文里的胶囊标签，随文本保存；附件条只有图片。只在输入框为
 * 空时恢复。返回是否正在恢复（此时不能提交）。
 */
function useComposerDraft(key: string | null): boolean {
  const aui = useAui();
  const text = useAuiState((state) => state.composer.text);
  const attachments = useAuiState((state) => state.composer.attachments);
  const ready = useRef(false);
  const [restoring, setRestoring] = useState(() => key !== null && Boolean(readComposerDraft(key)));
  useEffect(() => {
    if (!key) return;
    let active = true;
    ready.current = false;
    const unbind = bindComposerDraft(key, (draft) => {
      const result = applyEditDraftToComposer({ composer: aui.composer, text: draft.text,
        attachments: editableDraftAttachments(draft.attachments), mode: "prepend", isStillCurrent: () => active });
      // 写入已同步完成；保存最新值，不等 React 下次 effect 才记下已撤回内容。
      const state = aui.composer.getState();
      saveComposerDraft(key, { text: state.text, attachments: [...state.attachments] });
      return result;
    });
    const saved = readComposerDraft(key);
    const current = aui.composer.getState();
    void frontendLog("DEBUG", "composer_draft_restoring", "恢复前端草稿", {
      data: {
        key,
        savedLength: saved?.text.length,
        currentLength: current.text.length,
      },
    });
    // StrictMode 可在加入附件后重启 effect；由共享恢复器去重，仍需恢复正文。
    if (saved && !current.text) {
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
      unbind();
    };
  }, [aui, key]);
  // 卸载阶段 AUI 可能已释放 runtime，不能等 cleanup 再读取草稿。
  useEffect(() => {
    if (!key || !ready.current || restoring) return;
    saveComposerDraft(key, { text, attachments: [...attachments] });
  }, [key, text, attachments, restoring]);
  return restoring;
}

/** 主输入框：草稿 key 就是它所在的 Task。 */
export function useTaskComposerDraft(enabled: boolean): boolean {
  const target = useContext(ReferenceTargetScope);
  return useComposerDraft(enabled && target?.startsWith("task:") ? target : null);
}
