import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { LoaderCircleIcon, SquareIcon, UsersIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { cancelRun } from "@/lib/assistant/cancel-run";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { frontendLog } from "@/lib/logging/frontend-log";
import { ToolActivityRow } from "./tool-activity-row";
import { asRecord, readToolArtifact } from "./types";

type DelegationToolRowProps = ToolCallMessagePartProps & {
  /** Parent run context is supplied only by the writable main Thread. */
  runId?: number | null;
  /** The parent Run is already being cancelled. */
  runCancelling?: boolean;
};

/** Generic low-noise tool activity row adapter for delegation. */
export function DelegationToolRow({ artifact: rawArtifact, runId = null, runCancelling = false }: DelegationToolRowProps) {
  const artifact = readToolArtifact(rawArtifact);
  const data = asRecord(artifact.display_data);
  const actions = useWorkbenchActions();
  const title = typeof data.title === "string" && data.title.trim() ? data.title : "子 Agent";
  const role = artifact.agent_role ?? (typeof data.role === "string" ? data.role : null);
  const childTaskId = artifact.child_task_id ?? positiveId(data.child_task_id);
  const childRunId = artifact.child_run_id ?? positiveId(data.child_run_id);
  const isActive = artifact.backendStatus === "pending" || artifact.backendStatus === "running";
  const canOpen = childTaskId !== undefined && actions !== null && actions.workspaceId !== null;
  const canCancel = childTaskId !== undefined
    && childRunId !== undefined
    && isActive
    && runId !== null
    && !runCancelling;
  const [cancellationPending, setCancellationPending] = useState(false);
  const [cancellationError, setCancellationError] = useState<string | null>(null);

  useEffect(() => {
    if (!isActive) {
      setCancellationPending(false);
      setCancellationError(null);
    }
  }, [isActive]);

  const open = () => {
    if (canOpen && childTaskId !== undefined) {
      void frontendLog("INFO", "workbench_agent_open_requested", "主 Thread 请求打开子 Agent Workbench", {
        data: { childTaskId },
      });
      actions?.openAgent({ taskId: childTaskId, title, role });
    }
  };
  const requestCancellation = async () => {
    if (!canCancel || childTaskId === undefined || childRunId === undefined || cancellationPending) return;
    setCancellationPending(true);
    setCancellationError(null);
    void frontendLog("INFO", "delegation_child_run_cancel_requested", "用户请求停止子 Agent", {
      data: { parent_run_id: runId, child_task_id: childTaskId, child_run_id: childRunId },
    });

    const result = await cancelRun(null, childRunId);
    if (result.accepted || result.reason === "not_cancellable") {
      // A 409 means a cancellation signal already exists. In either case, wait
      // until the parent delegation tool part receives its canonical final status.
      void frontendLog("INFO", "delegation_child_run_cancel_signalled", "子 Agent 停止信号已存在或已发出", {
        data: {
          parent_run_id: runId,
          child_task_id: childTaskId,
          child_run_id: childRunId,
          already_signalled: !result.accepted,
        },
      });
      return;
    }

    setCancellationPending(false);
    setCancellationError(result.message);
    void frontendLog("WARNING", "delegation_child_run_cancel_failed", "停止子 Agent 请求失败", {
      data: {
        parent_run_id: runId,
        child_task_id: childTaskId,
        child_run_id: childRunId,
        reason: result.reason,
      },
    });
  };

  const cancelAction = canCancel ? (
    <Button
      type="button"
      size="sm"
      variant="ghost"
      className="h-7 shrink-0 gap-1.5 px-2 text-xs text-muted-foreground hover:text-destructive"
      aria-label={`${cancellationPending ? "正在停止" : "停止"}子 Agent：${title}`}
      title={cancellationError ?? (cancellationPending ? "正在等待子 Agent 收束" : `停止子 Agent：${title}`)}
      disabled={cancellationPending}
      onClick={() => void requestCancellation()}
    >
      {cancellationPending
        ? <LoaderCircleIcon className="size-3.5 animate-spin" aria-hidden="true" />
        : <SquareIcon className="size-3.5" aria-hidden="true" />}
      {cancellationPending ? "停止中" : "停止"}
    </Button>
  ) : null;

  return (
    <div className="min-w-0">
      <ToolActivityRow
        icon={<UsersIcon className="text-muted-foreground size-4 shrink-0" aria-hidden="true" />}
        title={title}
        meta={role ?? "角色未知"}
        status={artifact.backendStatus}
        onOpen={canOpen ? open : null}
        openLabel={`打开子 Agent：${title}`}
        disabled={!canOpen}
        action={cancelAction}
        className={artifact.backendStatus === "failed" ? "text-destructive" : undefined}
      />
      {isActive && cancellationError && <p className="px-2 pb-1 text-xs text-destructive" role="alert">{cancellationError}</p>}
    </div>
  );
}

function positiveId(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : undefined;
}
