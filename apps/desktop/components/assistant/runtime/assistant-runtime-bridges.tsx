import { useAui, useAuiState } from "@assistant-ui/react";
import type { CreateAttachment } from "@assistant-ui/core";
import { useEffect, type MutableRefObject } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";
import { readRuntimeTransportState, transportMessageCount } from "@/lib/assistant/transport-state-operations";
import type { ComposerRestore } from "@/components/assistant/runtime/runtime-types";
import type { PickedComposerAttachment } from "@/components/composer/attachment-picker";
import {
  getLocalAttachment,
  registerLocalAttachment,
  registerInlineFileMetadata,
} from "@/lib/assistant/attachments/local-attachment-registry";
import {
  applyEditDraftToComposer,
  beginEditComposerOperation,
  editableDraftAttachments,
  queuePendingEditComposerStart,
  setPendingEditComposerDraft,
} from "@/lib/assistant/edit-composer-draft";
import { inlineAttachmentTokenId } from "@/lib/assistant/attachments/local-file-token";
import { userErrorMessage } from "@/lib/errors/user-error-message";

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

export function InitialMessageBridge({
  text,
  attachments = [],
  sentRef,
  initialState,
  taskId,
  traceId,
  onError,
}: {
  text?: string;
  attachments?: readonly (PickedComposerAttachment | CreateAttachment)[];
  sentRef: MutableRefObject<boolean>;
  initialState: TransportState;
  taskId: number;
  traceId?: string;
  onError?: (message: string) => void;
}) {
  const aui = useAui();

  useEffect(() => {
    if (
      !text?.trim() && attachments.length === 0
      || sentRef.current
      || transportMessageCount(initialState) > 0
      || initialState.activity !== null
    ) return;
    const composer = aui.thread.composer();
    composer.setText(text ?? "");
    let cancelled = false;
    const reportError = (error: unknown, stage: "add_attachment" | "send") => {
      if (cancelled) return;
      const message = userErrorMessage(error, "初始消息发送失败，请重试");
      void frontendLog("ERROR", "assistant_initial_message_failed", "初始消息发送失败", {
        traceId,
        data: { taskId, attachmentCount: attachments.length, stage },
        error,
      });
      onError?.(message);
    };

    void (async () => {
      try {
        for (const attachment of attachments) {
          if (cancelled) return;
          if ("path" in attachment && !getLocalAttachment(attachment.file)) {
            registerLocalAttachment(attachment.file, {
              id: attachment.id,
              path: attachment.path,
              name: attachment.name,
              contentType: attachment.file.type,
              kind: attachment.kind,
            });
          }
          if ("path" in attachment) {
            if (attachment.kind === "image") await composer.addAttachment(attachment.file);
          } else if (attachment.type === "file") {
            for (const file of editableDraftAttachments([attachment])) {
              registerInlineFileMetadata({
                id: inlineAttachmentTokenId(file),
                name: file.name,
                contentType: file.contentType ?? "application/octet-stream",
              });
            }
          } else {
            await composer.addAttachment(attachment);
          }
        }
        const timer = window.setTimeout(() => {
          if (cancelled || sentRef.current) return;
          try {
            sentRef.current = true;
            void Promise.resolve(composer.send()).catch((error: unknown) => {
              sentRef.current = false;
              reportError(error, "send");
            });
          } catch (error) {
            sentRef.current = false;
            reportError(error, "send");
          }
        }, 0);
        if (cancelled) window.clearTimeout(timer);
      } catch (error) {
        reportError(error, "add_attachment");
      }
    })();
    return () => { cancelled = true; };
  }, [attachments, aui, initialState, onError, sentRef, taskId, text, traceId]);

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
      restoreEditMessage: (sourceId, intent, text, attachments = []) => {
        try {
          beginEditComposerOperation(sourceId);
          setPendingEditComposerDraft(sourceId, { text, attachments, intent });
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
