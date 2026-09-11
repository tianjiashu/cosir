import { useEffect, useRef } from "react";

import { currentTransportRun, transportMessageCount } from "@/lib/assistant/transport-state-operations";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { RuntimeSessionContext } from "@/components/assistant/runtime/runtime-types";

/** Record runtime lifecycle transitions without making diagnostics business state. */
export function useRuntimeDiagnostics(context: RuntimeSessionContext): void {
  const mountedRef = useRef(false);
  const unmountLogTimerRef = useRef<number | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    if (unmountLogTimerRef.current !== null) {
      window.clearTimeout(unmountLogTimerRef.current);
      unmountLogTimerRef.current = null;
    }

    void frontendLog("INFO", "assistant_runtime_mounted", "Assistant runtime 已挂载", {
      traceId: context.traceId,
      data: {
        taskId: context.taskId,
        workspaceId: context.workspaceId ?? null,
        initialMessageCount: transportMessageCount(context.initialState),
        initialRunId: context.initialState.current_run_id,
        initialRunStatus: currentTransportRun(context.initialState)?.status ?? null,
      },
    });

    return () => {
      mountedRef.current = false;
      // React StrictMode deliberately performs an effect cleanup/setup pair in
      // development. Delay this diagnostic so that pair is not a real teardown.
      unmountLogTimerRef.current = window.setTimeout(() => {
        unmountLogTimerRef.current = null;
        if (mountedRef.current) return;
        void frontendLog("WARNING", "assistant_runtime_unmounted", "Assistant runtime 生命周期已清理", {
          traceId: context.traceId,
          data: {
            taskId: context.taskId,
            workspaceId: context.workspaceId ?? null,
            lastRunId: context.latestStateRef.current.current_run_id,
            lastRunStatus: currentTransportRun(context.latestStateRef.current)?.status ?? null,
          },
        });
      }, 0);
    };
  }, [context]);
}
