import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { memo, useCallback, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import { type TransportIssue } from "@/components/assistant/transport-status";
import {
  ComposerRestoreBridge,
  InitialMessageBridge,
  RuntimeControlBridge,
  RuntimeRenderDiagnostics,
  TaskStateBridge,
  TransportStateCommitBridge,
} from "@/components/assistant/runtime/assistant-runtime-bridges";
import { useRuntimeCancellation } from "@/components/assistant/runtime/use-runtime-cancellation";
import { useRuntimeDiagnostics } from "@/components/assistant/runtime/use-runtime-diagnostics";
import { useRuntimeRecovery } from "@/components/assistant/runtime/use-runtime-recovery";
import { useRuntimeTransport } from "@/components/assistant/runtime/use-runtime-transport";
import type {
  AssistantRuntimeProps,
  ComposerRestore,
  RuntimeControls,
  RuntimeSessionContext,
} from "@/components/assistant/runtime/runtime-types";
import { currentTransportRun } from "@/lib/assistant/transport-state-operations";
import { newTraceId } from "@/lib/trace";
import {
  getBackendRuntimeSnapshot,
  subscribeBackendRuntime,
} from "@/src/runtime-config";

type RuntimeSessionProps = AssistantRuntimeProps & {
  setIssue: (issue: TransportIssue | null) => void;
};

/** Compose the backend transport runtime, lifecycle hooks, bridges, and thread UI. */
export const AssistantRuntimeSession = memo(function AssistantRuntimeSession({
  taskId,
  workspaceId,
  workspaceRoot,
  initialState,
  setIssue,
  initialMessage,
  initialAttachments,
  forkAvailable,
  forkingRunId,
  onForkRun,
  onTaskStateChanged,
  onRunStateChange,
}: RuntimeSessionProps) {
  const backendRuntime = useSyncExternalStore(
    subscribeBackendRuntime,
    getBackendRuntimeSnapshot,
    getBackendRuntimeSnapshot,
  );
  const { backendBaseUrl, generation: backendRuntimeGeneration } = backendRuntime;
  const [traceId] = useState(() => newTraceId());
  const initialStateRef = useRef(initialState);
  const sessionInitialState = initialStateRef.current;
  const latestStateRef = useRef(sessionInitialState);
  const runtimeControlsRef = useRef<RuntimeControls | null>(null);
  const cancelRequestedRunIdRef = useRef<number | null>(null);
  const lastTransportErrorRef = useRef<TransportIssue | null>(null);
  const composerRestoreRef = useRef<ComposerRestore | null>(null);
  const initialMessageSentRef = useRef(false);
  const onTaskStateChangedRef = useRef(onTaskStateChanged);
  onTaskStateChangedRef.current = onTaskStateChanged;

  const registerRuntimeControls = useCallback((controls: RuntimeControls | null) => {
    runtimeControlsRef.current = controls;
  }, []);
  const registerComposerRestore = useCallback((restore: ComposerRestore) => {
    composerRestoreRef.current = restore;
  }, []);
  const handleInitialMessageError = useCallback((message: string) => {
    setIssue({ message, retryable: true });
  }, [setIssue]);
  const notifyTaskStateChanged = useCallback(() => {
    onTaskStateChangedRef.current?.();
  }, []);

  const context = useMemo<RuntimeSessionContext>(() => ({
    taskId,
    workspaceId,
    workspaceRoot,
    initialState: sessionInitialState,
    backendBaseUrl,
    backendRuntimeGeneration,
    traceId,
    setIssue,
    onTaskStateChanged: notifyTaskStateChanged,
    latestStateRef,
    runtimeControlsRef,
    cancelRequestedRunIdRef,
    lastTransportErrorRef,
    composerRestoreRef,
  }), [
    backendBaseUrl,
    backendRuntimeGeneration,
    notifyTaskStateChanged,
    sessionInitialState,
    setIssue,
    taskId,
    traceId,
    workspaceId,
    workspaceRoot,
  ]);

  useRuntimeDiagnostics(context);
  const cancellation = useRuntimeCancellation(context);
  const commitTransportState = useCallback((state: typeof sessionInitialState) => {
    latestStateRef.current = state;
    cancellation.onStateCommitted(state);
  }, [cancellation.onStateCommitted, sessionInitialState]);
  const recovery = useRuntimeRecovery(context);
  const runtime = useRuntimeTransport(context, recovery);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RuntimeControlBridge
        register={registerRuntimeControls}
        resumeOnMount={currentTransportRun(sessionInitialState)?.status === "pending" || currentTransportRun(sessionInitialState)?.status === "running"}
        taskId={taskId}
      />
      <TransportStateCommitBridge initialState={sessionInitialState} onCommit={commitTransportState} />
      <ComposerRestoreBridge register={registerComposerRestore} />
      <TaskStateBridge onRunStateChange={onRunStateChange} />
      <RuntimeRenderDiagnostics taskId={taskId} />
      <div className="flex h-full min-h-0 flex-col">
        <Thread
          taskId={taskId}
          workspaceRoot={workspaceRoot}
          forkAvailable={forkAvailable}
          forkingRunId={forkingRunId}
          onForkRun={onForkRun}
          onResumeBusiness={recovery.resumeBusinessRun}
          onCancelRequested={cancellation.onRequested}
          onCancelResult={cancellation.onResult}
          cancellingRunId={cancellation.cancellingRunId}
        />
      </div>
      <InitialMessageBridge
        text={initialMessage}
        attachments={initialAttachments}
        sentRef={initialMessageSentRef}
        initialState={sessionInitialState}
        taskId={taskId}
        traceId={traceId}
        onError={handleInitialMessageError}
      />
    </AssistantRuntimeProvider>
  );
});
