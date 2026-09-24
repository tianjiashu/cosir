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
import { readDelegationDisplay } from "./child-agent-display";
import { readToolArtifact } from "./types";
import { useToolDisclosure } from "./tool-disclosure";
import { DARK_TOOL_CARD_CONTENT_CLASS, DARK_TOOL_CARD_HEADER_CLASS } from "../elements/tool-layout-tokens";

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

  // Locator and lifecycle fields stay in the transport artifact for opening,
  // cancellation, and child-run convergence. They are intentionally not
  // repeated in the compact row because ToolStatus is their single UI owner.
  const meta = role ? <span className="max-w-[55%] truncate text-zinc-400">{role}</span> : undefined;

  return (
    <Collapsible open={isOpen} onOpenChange={setOpen} className="group/tool-call overflow-hidden rounded-xl border border-white/10 bg-zinc-900 text-zinc-100 shadow-sm">
      <div className={DARK_TOOL_CARD_HEADER_CLASS}>
        <DisclosureRow
          leading={<UsersIcon className="size-3.5 text-zinc-400" aria-hidden="true" />}
          label={<span className="truncate text-sm font-medium" title={title}>{title}</span>}
          meta={meta}
          trailing={<ToolStatus status={status} className="text-zinc-400" />}
          tone="terminal"
          className="w-auto min-w-0 flex-1 border-b-0 bg-transparent px-3"
          data-testid="tool-activity-row"
          aria-label={`展开子 Agent详情：${title}`}
        />
        {canOpen && (
          <Button type="button" variant="ghost" size="sm" className="h-7 shrink-0 px-2 text-zinc-400 hover:bg-white/10 hover:text-zinc-100" aria-label={`打开子 Agent工作台：${title}`} onClick={open}>
            打开
          </Button>
        )}
        {cancelAction}
      </div>
      <CollapsibleContent className={DARK_TOOL_CARD_CONTENT_CLASS}>
        <div className="space-y-1.5 border-t border-white/5 px-3 py-2 font-mono text-xs text-zinc-300">
          {status === "completed" && <p className="text-zinc-400">子 Agent 已完成；打开工作台查看完整结果。</p>}
          {status === "failed" && <p className="text-red-300">子 Agent 执行失败。</p>}
          {status === "cancelled" && <p className="text-zinc-400">子 Agent 已取消。</p>}
          {cancellationError && <p className="text-amber-300" role="alert">{cancellationError}</p>}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
