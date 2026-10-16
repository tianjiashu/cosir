import { useAui, useAuiState } from "@assistant-ui/react";
import type { CreateAttachment } from "@assistant-ui/core";
import { useEffect, useRef, type MutableRefObject } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";
import { currentTransportRun, transportMessageCount } from "@/lib/assistant/transport-state-operations";
import type {
  ComposerRestore,
  RuntimeControls,
} from "@/components/assistant/runtime/runtime-types";
import type { PickedComposerAttachment } from "@/components/composer/attachment-picker";
import type { BackendRuntimeSnapshot } from "@/src/runtime-config";
import {
  getLocalAttachment,
  registerLocalAttachment,
} from "@/lib/assistant/attachments/local-attachment-registry";
import {
  applyEditDraftToComposer,
  beginEditComposerOperation,
  editableDraftAttachments,
  isCurrentEditComposerOperation,
  queuePendingEditComposerStart,
  setPendingEditComposerDraft,
} from "@/lib/assistant/edit-composer-draft";
import { userErrorMessage } from "@/lib/errors/user-error-message";

export function RuntimeRenderDiagnostics({ taskId }: { taskId: number }) {
  const messageCount = useAuiState((state) => state.thread.messages.length);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const draftLength = useAuiState((state) => state.composer.text.length);
  const runId = useAuiState((state) => {
    const run = readRuntimeRun(state.thread.state);
    return typeof run?.runId === "number" ? run.runId : null;
  });
  const runStatus = useAuiState((state) => {
    const run = readRuntimeRun(state.thread.state);
    return typeof run?.status === "string" ? run.status : "unknown";
  });

  useEffect(() => {
    void frontendLog("DEBUG", "assistant_ui_render_state", "Assistant UI 渲染状态发生变化", {
      data: { taskId, messageCount, runId, runStatus, isRunning, draftLength },
    });
  }, [draftLength, isRunning, messageCount, runId, runStatus, taskId]);

  return null;
}

type RuntimeRunDiagnostic = {
  runId?: unknown;
  status?: unknown;
};

function readRuntimeRun(value: unknown): RuntimeRunDiagnostic | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const state = value as { current_run_id?: unknown; runs?: unknown };
  if (!Array.isArray(state.runs) || typeof state.current_run_id !== "number") return undefined;
  const run = state.runs.find((candidate) =>
    typeof candidate === "object" && candidate !== null
      && (candidate as { runId?: unknown }).runId === state.current_run_id,
  );
  return typeof run === "object" && run !== null ? run as RuntimeRunDiagnostic : undefined;
}

/**
 * 向会话注册 runtime 控制（resume / importState），并在挂载时为仍在运行的 Run 发起一次 attach。
 *
 * 挂载时后端不可用则不发起：后端恢复后由会话的后端变化入口（recovery.resync）统一对账并续订，
 * 避免这里与恢复入口各自 attach 一次。
 */
export function RuntimeControlBridge({
  register,
  backendRef,
  resumeOnMount,
  attachTransportRef,
}: {
  register: (controls: RuntimeControls | null) => void;
  backendRef: MutableRefObject<BackendRuntimeSnapshot>;
  resumeOnMount: boolean;
  attachTransportRef: MutableRefObject<(() => Promise<void>) | null>;
}) {
  const aui = useAui();
  const initialResumeIssuedRef = useRef(false);

  useEffect(() => {
    register({
      resume: () => {
        if (backendRef.current.available) void attachTransportRef.current?.();
      },
      importState: (state) => aui.thread.importExternalState(state),
    });
    return () => register(null);
  }, [attachTransportRef, aui, backendRef, register]);

  useEffect(() => {
    if (!backendRef.current.available || !resumeOnMount || initialResumeIssuedRef.current) return;
    let cancelled = false;
    const scheduledBackendGeneration = backendRef.current.generation;
    const timer = window.setTimeout(() => {
      if (
        cancelled
        || !backendRef.current.available
        || backendRef.current.generation !== scheduledBackendGeneration
        || initialResumeIssuedRef.current
      ) return;
      initialResumeIssuedRef.current = true;
      void attachTransportRef.current?.();
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [attachTransportRef, backendRef, resumeOnMount]);

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
      || (currentTransportRun(initialState)?.status ?? "idle") !== "idle"
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
          await composer.addAttachment("path" in attachment ? attachment.file : attachment);
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
      messageId?: string,
      generation?: number,
    ) => {
      const result = await applyEditDraftToComposer({
        composer,
        text,
        attachments: editableDraftAttachments(attachments),
        isStillCurrent: messageId !== undefined && generation !== undefined
          ? () => isCurrentEditComposerOperation(messageId, generation)
          : () => true,
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
