import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { LoaderCircleIcon, SquareIcon, UsersIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { cancelRun } from "@/lib/assistant/cancel-run";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { frontendLog } from "@/lib/logging/frontend-log";
import { DisclosureRow } from "../elements/disclosure-row.aui";
import { ToolStatus } from "./tool-status";
import { childAgentStatusLabel, readDelegationDisplay } from "./child-agent-display";
import { readToolArtifact } from "./types";
import { useToolDisclosure } from "./tool-disclosure";

type DelegationToolRowProps = ToolCallMessagePartProps & {
  /** Parent run context is supplied only by the writable main Thread. */
  runId?: number | null;
  /** The parent Run is already being cancelled. */
  runCancelling?: boolean;
};

/** Generic low-noise tool activity row adapter for delegation. */
export function DelegationToolRow({ artifact: rawArtifact, runId = null, runCancelling = false }: DelegationToolRowProps) {
  const artifact = readToolArtifact(rawArtifact);
  const display = readDelegationDisplay(artifact.display_data);
  const actions = useWorkbenchActions();
  const title = display?.title ?? "子 Agent";
  const role = artifact.agent_role ?? display?.role ?? null;
  const childTaskId = artifact.child_task_id ?? display?.childTaskId;
  const childRunId = artifact.child_run_id ?? display?.childRunId;
  const status = display?.status ?? artifact.backendStatus;
  const isActive = status === "pending" || status === "running";
  const canOpen = childTaskId !== undefined && actions !== null && actions.workspaceId !== null;
  const canCancel = childTaskId !== undefined
    && childRunId !== undefined
    && isActive
    && runId !== null
    && !runCancelling;
  const [cancellationPending, setCancellationPending] = useState(false);
  const [cancellationError, setCancellationError] = useState<string | null>(null);
  const [isOpen, setOpen] = useToolDisclosure(status, false, { openWhileRunning: false });

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

  const sessionReference = [
    childTaskId === undefined ? null : `task ${childTaskId}`,
    childRunId === undefined ? null : `run ${childRunId}`,
  ].filter(Boolean).join(" · ");
  const lifecycleLabel = status === "unknown" ? "状态未知" : childAgentStatusLabel(status);
  const meta = [
    role ?? "角色未知",
    sessionReference,
    lifecycleLabel,
    display?.finalOutput ?? display?.statusHint,
  ].filter(Boolean).join(" · ");
  const output = display?.finalOutput;
  const hint = display?.statusHint;

  return (
    <Collapsible open={isOpen} onOpenChange={setOpen} className="group/tool-call my-1 overflow-hidden rounded-xl border border-white/10 bg-zinc-900 text-zinc-100 shadow-sm">
      <div className="flex min-w-0 items-center gap-1 border-b border-white/5 bg-white/[0.02] pr-2">
        <DisclosureRow
          leading={<UsersIcon className="size-3.5 text-zinc-400" aria-hidden="true" />}
          label={<span className="truncate text-sm font-medium" title={title}>{title}</span>}
          meta={<span className="max-w-[55%] truncate text-zinc-400">{meta}</span>}
          trailing={<ToolStatus status={status} className="text-zinc-400" />}
          tone="terminal"
          className="w-auto min-w-0 flex-1 border-b-0 bg-transparent px-3"
          data-testid="tool-activity-row"
          aria-label={`子 Agent：${title}`}
          onClick={(event) => {
            if (canOpen) {
              event.preventDefault();
              open();
            }
          }}
        />
        {canOpen && (
          <Button type="button" variant="ghost" size="sm" className="h-7 shrink-0 px-2 text-zinc-400 hover:bg-white/10 hover:text-zinc-100" onClick={open}>
            打开
          </Button>
        )}
        {cancelAction}
      </div>
      <CollapsibleContent className="ml-6 pb-2 pl-2 pr-2">
        <div className="space-y-1.5 border-t border-white/5 px-3 py-2 font-mono text-xs text-zinc-300">
          {sessionReference && <p className="text-zinc-500">{sessionReference}</p>}
          {hint && <p className="text-amber-300">{hint}</p>}
          {status === "completed" && output && <p className="whitespace-pre-wrap break-words text-zinc-200">{output}</p>}
          {(status === "failed" || status === "cancelled") && !hint && <p className={status === "failed" ? "text-red-300" : "text-zinc-400"}>{status === "failed" ? "子 Agent 执行失败" : "子 Agent 已取消"}</p>}
          {cancellationError && <p className="text-amber-300" role="alert">{cancellationError}</p>}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
