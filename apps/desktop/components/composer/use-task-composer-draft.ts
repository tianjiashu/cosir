import { useContext, useEffect, useRef, useState } from "react";
import { useAui, useAuiState } from "@assistant-ui/react";
import type { Attachment } from "@assistant-ui/core";
import { toast } from "sonner";
import { ReferenceTargetScope } from "@/components/workspace-references/reference-bridge";
import {
  applyEditDraftToComposer,
  editableDraftAttachments,
} from "@/lib/assistant/edit-composer-draft";
import { frontendLog } from "@/lib/logging/frontend-log";

const drafts = new Map<
  string,
  { text: string; attachments: readonly Attachment[] }
>();

/** 在 AUI 仍存活时保存整个前端草稿；引用仍只是 text，普通附件复用 AUI 的恢复入口。 */
export function useTaskComposerDraft(enabled: boolean): boolean {
  const aui = useAui();
  const text = useAuiState((state) => state.composer.text);
  const attachments = useAuiState((state) => state.composer.attachments);
  const ready = useRef(false);
  const target = useContext(ReferenceTargetScope);
  const key = enabled && target?.startsWith("task:") ? target : null;
  const [restoring, setRestoring] = useState(Boolean(key && drafts.has(key)));
  useEffect(() => {
    if (!key) return;
    let active = true;
    ready.current = false;
    const saved = drafts.get(key);
    const current = aui.composer.getState();
    void frontendLog("DEBUG", "task_draft_restoring", "恢复前端草稿", {
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
            (attachment) => attachment.status.type === "complete",
          );
          const result = await applyEditDraftToComposer({
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
            if (attachment.status.type !== "complete" && attachment.file)
              await aui.composer.addAttachment(attachment.file);
          }
        } catch (error) {
          if (active) {
            toast.error("部分草稿附件恢复失败，请重新添加");
            void frontendLog(
              "WARNING",
              "task_draft_restore_failed",
              "Task 草稿附件恢复失败",
              { error, data: { target: key } },
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
    };
  }, [aui, key]);
  // 卸载阶段 AUI 可能已释放 runtime，不能等 cleanup 再读取草稿。
  useEffect(() => {
    if (!key || !ready.current || restoring) return;
    if (text || attachments.length)
      drafts.set(key, { text, attachments: [...attachments] });
    else drafts.delete(key);
  }, [key, text, attachments, restoring]);
  return restoring;
}
