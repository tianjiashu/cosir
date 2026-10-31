import { useAui, useAuiState } from "@assistant-ui/react";
import type { CreateAttachment } from "@assistant-ui/core";
import { useEffect } from "react";

import { frontendLog } from "@/lib/logging/frontend-log";
import { readRuntimeTransportState } from "@/lib/assistant/transport-state-operations";
import type { ComposerRestore } from "@/components/assistant/runtime/runtime-types";
import {
  applyEditDraftToComposer,
  beginEditComposerOperation,
  editableDraftAttachments,
  queuePendingEditComposerStart,
  setPendingEditComposerDraft,
} from "@/lib/assistant/edit-composer-draft";

export function RuntimeRenderDiagnostics({ taskId }: { taskId: number }) {
  const messageCount = useAuiState((state) => state.thread.messages.length);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const activityRunId = useAuiState((state) => readRuntimeTransportState(state.thread.state)?.activity?.runId ?? null);
  const activityStatus = useAuiState((state) => readRuntimeTransportState(state.thread.state)?.activity?.status ?? null);

  useEffect(() => {
    void frontendLog("DEBUG", "assistant_ui_render_state", "Assistant UI 渲染状态发生变化", {
      data: { taskId, messageCount, activityRunId, activityStatus, isRunning },
    });
  }, [activityRunId, activityStatus, isRunning, messageCount, taskId]);

  return null;
}

export function ComposerRestoreBridge({
  register,
}: {
  register: (restore: ComposerRestore) => void;
}) {
  const aui = useAui();

  useEffect(() => {
    // 草稿写入只保留一份实现（applyEditDraftToComposer）：新增消息与失败恢复共用同一
    // 顺序与同一去重规则，避免两条路径各自演化出「文本有 token、附件缺失」的中间态。
    const restoreIntoComposer = async (
      composer: ReturnType<typeof aui.thread.composer>,
      text: string,
      attachments: readonly CreateAttachment[],
    ) => {
      const result = await applyEditDraftToComposer({
        composer,
        text,
        attachments: editableDraftAttachments(attachments),
        // 新增消息的恢复没有编辑会话可被替代，始终有效。
        isStillCurrent: () => true,
      });
      if (result.error) {
        void frontendLog("ERROR", "assistant_composer_restore_failed", "失败消息恢复附件失败", {
          data: {
            attachmentCount: result.expectedCount,
            addedCount: result.addedCount,
            superseded: result.superseded,
          },
          error: result.error,
        });
      }
    };
    register({
      restoreNewMessage: (text, attachments = []) => {
        const composer = aui.thread.composer();
        void restoreIntoComposer(composer, text, attachments);
      },
      restoreEditMessage: (sourceId, text, attachments = []) => {
        try {
          beginEditComposerOperation(sourceId);
          setPendingEditComposerDraft(sourceId, { text, attachments });
          queuePendingEditComposerStart(sourceId);
          return true;
        } catch (error) {
          void frontendLog("WARNING", "assistant_edit_composer_restore_rejected", "编辑草稿恢复未能交给输入框", {
            data: { sourceId },
            error,
          });
          return false;
        }
      },
    });
  }, [aui, register]);

  return null;
}
