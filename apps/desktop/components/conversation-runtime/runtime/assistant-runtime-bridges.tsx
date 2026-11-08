import { useAuiState } from "@assistant-ui/react";
import { useEffect } from "react";

import { frontendLog } from "@/lib/logging/frontend-log";
import { readRuntimeTransportState } from "@/lib/assistant/transport/transport-state-operations";

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
