import { useAui, useAuiState } from "@assistant-ui/react";
import type { CreateAttachment } from "@assistant-ui/core";
import { useEffect, useLayoutEffect, useRef, type MutableRefObject } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import { frontendLog, safeFrontendErrorMessage } from "@/lib/logging/frontend-log";
import {
  currentTransportRun,
  isTransportState,
  transportMessageCount,
} from "@/lib/assistant/transport-state-operations";
import type {
  AssistantRuntimeProps,
  ComposerRestore,
  RuntimeControls,
} from "@/components/assistant/runtime/runtime-types";
import type { PickedComposerAttachment } from "@/components/composer/attachment-picker";
import {
  getLocalAttachment,
  registerLocalAttachment,
} from "@/lib/assistant/attachments/local-attachment-registry";
import {
  applyEditDraftToComposer,
  beginEditComposerOperation,
  discardPendingEditComposerDraft,
  editableDraftAttachments,
  endEditComposerOperation,
  isCurrentEditComposerOperation,
  setPendingEditComposerDraft,
} from "@/lib/assistant/edit-composer-draft";

type TransportStateCommitBridgeProps = {
  initialState: TransportState;
  onCommit: (state: TransportState) => void;
};

export function TransportStateCommitBridge({
  initialState,
  onCommit,
}: TransportStateCommitBridgeProps) {
  const state = useAuiState((runtimeState) => runtimeState.thread.state);
  const lastValidStateRef = useRef(initialState);

  useLayoutEffect(() => {
    if (isTransportState(state)) {
      lastValidStateRef.current = state;
      onCommit(state);
      return;
    }
    onCommit(lastValidStateRef.current);
  }, [initialState, onCommit, state]);

  return null;
}

export function TaskStateBridge({
  onRunStateChange,
}: Pick<AssistantRuntimeProps, "onRunStateChange">) {
  const isRunning = useAuiState((state) => state.thread.isRunning);

  useEffect(() => {
    onRunStateChange?.(isRunning);
  }, [isRunning, onRunStateChange]);

  return null;
}

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

export function RuntimeControlBridge({
  register,
  resumeOnMount,
  taskId,
}: {
  register: (controls: RuntimeControls | null) => void;
  resumeOnMount: boolean;
  taskId: number;
}) {
  const aui = useAui();
  const remoteThreadId = useAuiState((state) => state.threadListItem.remoteId);
  const initialResumeIssuedRef = useRef(false);

  useEffect(() => {
    register({
      resume: () => aui.thread.resumeRun({ parentId: null }),
      importState: (state) => aui.thread.importExternalState(state),
    });
    return () => register(null);
  }, [aui, register]);

  useEffect(() => {
    if (!resumeOnMount || initialResumeIssuedRef.current || remoteThreadId !== `task-${taskId}`) return;
    const timer = window.setTimeout(() => {
      if (initialResumeIssuedRef.current) return;
      initialResumeIssuedRef.current = true;
      void aui.thread.resumeRun({ parentId: null });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [aui, remoteThreadId, resumeOnMount, taskId]);

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
  const remoteThreadId = useAuiState((state) => state.threadListItem.remoteId);

  useEffect(() => {
    if (
      !text?.trim() && attachments.length === 0
      || sentRef.current
      || remoteThreadId !== `task-${taskId}`
      || transportMessageCount(initialState) > 0
      || (currentTransportRun(initialState)?.status ?? "idle") !== "idle"
    ) return;
    const composer = aui.thread.composer();
    composer.setText(text ?? "");
    let cancelled = false;
    const reportError = (error: unknown, stage: "add_attachment" | "send") => {
      if (cancelled) return;
      const message = safeFrontendErrorMessage(error, "初始消息发送失败，请重试");
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
  }, [attachments, aui, initialState, onError, remoteThreadId, sentRef, taskId, text, traceId]);

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
          const composer = aui.thread.message({ id: sourceId }).composer();
          const generation = beginEditComposerOperation(sourceId);
          if (!composer.getState().isEditing) {
            setPendingEditComposerDraft(sourceId, { text, attachments });
            try {
              composer.beginEdit();
            } catch (error) {
              discardPendingEditComposerDraft(sourceId);
              endEditComposerOperation(sourceId, generation);
              throw error;
            }
          } else {
            void restoreIntoComposer(composer, text, attachments, sourceId, generation)
              .finally(() => endEditComposerOperation(sourceId, generation));
          }
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
