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
import { LOCAL_FILE_TOKEN } from "@/lib/assistant/attachments/local-file-token";

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
    const restoreAttachments = async (
      composer: ReturnType<typeof aui.thread.composer>,
      attachments: readonly CreateAttachment[],
    ): Promise<void> => {
      for (const attachment of attachments) {
        await composer.addAttachment(attachment);
      }
    };
    const restoreIntoComposer = async (
      composer: ReturnType<typeof aui.thread.composer>,
      text: string,
      attachments: readonly CreateAttachment[],
    ) => {
      try {
        await restoreAttachments(composer, attachments);
        composer.setText(text);
      } catch (error) {
        // Do not leave an unresolved internal token in the editable value when
        // an attachment cannot be restored. The transport error already owns
        // the user-facing failure state; this fallback only keeps the draft
        // readable and guarantees a handled Promise rejection.
        composer.setText(text.replace(LOCAL_FILE_TOKEN, "附件"));
        void frontendLog("ERROR", "assistant_composer_restore_failed", "失败消息恢复附件失败", {
          error,
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
          if (!composer.getState().isEditing) composer.beginEdit();
          void restoreIntoComposer(composer, text, attachments);
          return true;
        } catch {
          return false;
        }
      },
    });
  }, [aui, register]);

  return null;
}
