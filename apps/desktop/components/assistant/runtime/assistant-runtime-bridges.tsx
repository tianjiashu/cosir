import { useAui, useAuiState } from "@assistant-ui/react";
import { useEffect, useLayoutEffect, useRef, type MutableRefObject } from "react";

import type { TransportState } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";
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
  sentRef,
  initialState,
  taskId,
}: {
  text?: string;
  sentRef: MutableRefObject<boolean>;
  initialState: TransportState;
  taskId: number;
}) {
  const aui = useAui();
  const remoteThreadId = useAuiState((state) => state.threadListItem.remoteId);

  useEffect(() => {
    if (
      !text?.trim()
      || sentRef.current
      || remoteThreadId !== `task-${taskId}`
      || transportMessageCount(initialState) > 0
      || (currentTransportRun(initialState)?.status ?? "idle") !== "idle"
    ) return;
    aui.thread.composer().setText(text);
    const timer = window.setTimeout(() => {
      if (sentRef.current) return;
      sentRef.current = true;
      aui.thread.composer().send();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [aui, initialState, remoteThreadId, sentRef, taskId, text]);

  return null;
}

export function ComposerRestoreBridge({
  register,
}: {
  register: (restore: ComposerRestore) => void;
}) {
  const aui = useAui();

  useEffect(() => {
    register({
      restoreNewMessage: (text) => aui.thread.composer().setText(text),
      restoreEditMessage: (sourceId, text) => {
        try {
          const composer = aui.thread.message({ id: sourceId }).composer();
          if (!composer.getState().isEditing) composer.beginEdit();
          composer.setText(text);
          return true;
        } catch {
          return false;
        }
      },
    });
  }, [aui, register]);

  return null;
}
