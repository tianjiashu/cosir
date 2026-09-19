"use client";

import { useEffect, useRef, useState } from "react";

import { TransportStatus, type TransportIssue } from "@/components/assistant/transport-status";
import { AssistantRuntimeErrorBoundary } from "@/components/assistant/runtime/assistant-runtime-error-boundary";
import { AssistantRuntimeSession } from "@/components/assistant/runtime/assistant-runtime-session";
import type { AssistantRuntimeProps } from "@/components/assistant/runtime/runtime-types";
import { createTimeoutAbort } from "@/lib/async/abort-timeout";
import { requestAssistantSnapshot } from "@/lib/assistant/assistant-snapshot-client";
import type { TransportState } from "@/lib/assistant/contract";
import { safeFrontendErrorMessage } from "@/lib/logging/frontend-log";

const RETRY_STATE_TIMEOUT_MS = 15_000;

export type { AssistantRuntimeProps } from "@/components/assistant/runtime/runtime-types";

/** Public task-scoped shell for the Assistant UI transport session. */
export function AssistantRuntime(props: AssistantRuntimeProps) {
  const [issue, setIssue] = useState<TransportIssue | null>(null);
  const [runtimeGeneration, setRuntimeGeneration] = useState(0);
  const [retryState, setRetryState] = useState<TransportState | null>(null);
  const retryAbortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => retryAbortControllerRef.current?.abort(), []);

  const handleRetry = () => {
    retryAbortControllerRef.current?.abort();
    const { controller, signal, clear } = createTimeoutAbort(RETRY_STATE_TIMEOUT_MS);
    retryAbortControllerRef.current = controller;
    void requestAssistantSnapshot(props.taskId, { signal })
      .then((state) => {
        if (controller.signal.aborted) return;
        setRetryState(state);
        setIssue(null);
        setRuntimeGeneration((generation) => generation + 1);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setIssue({
          message: safeFrontendErrorMessage(error, "无法读取本机后端的最新对话状态"),
          canResync: true,
        });
      })
      .finally(() => {
        clear();
        if (retryAbortControllerRef.current === controller) retryAbortControllerRef.current = null;
      });
  };

  const initialState = retryState ?? props.initialState;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <TransportStatus issue={issue} onRetry={handleRetry} />
      <div className="min-h-0 flex-1">
        <AssistantRuntimeErrorBoundary
          key={runtimeGeneration}
          taskId={props.taskId}
          onRetry={handleRetry}
        >
          <AssistantRuntimeSession {...props} initialState={initialState} setIssue={setIssue} />
        </AssistantRuntimeErrorBoundary>
      </div>
    </div>
  );
}
