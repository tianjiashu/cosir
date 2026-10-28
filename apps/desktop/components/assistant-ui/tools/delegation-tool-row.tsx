import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { CirclePauseIcon, LoaderCircleIcon, SquareIcon, UsersIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { taskRunStatusPresentation } from "@/components/task-tree/task-run-status";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { cancelTask } from "@/lib/assistant/task-commands";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { frontendLog } from "@/lib/logging/frontend-log";
import { DisclosureRow } from "../elements/disclosure-row.aui";
import { ToolStatus } from "./tool-status";
import { readDelegationDisplay } from "./child-agent-display";
import { readToolArtifact } from "./types";
import { useToolDisclosure } from "./tool-disclosure";
import { DARK_TOOL_CARD_CONTENT_CLASS, DARK_TOOL_CARD_HEADER_CLASS } from "../elements/tool-layout-tokens";

type DelegationToolRowProps = ToolCallMessagePartProps & {
  /** 只有可写主 Thread 提供停止子 Agent 的入口。 */
  interactive?: boolean;
  /** 父 Task 的当前活动已在停止中。 */
  taskCancelling?: boolean;
};

/** Generic low-noise tool activity row adapter for delegation. */
export function DelegationToolRow({ artifact: rawArtifact, interactive = false, taskCancelling = false }: DelegationToolRowProps) {
  const artifact = readToolArtifact(rawArtifact);
  const display = readDelegationDisplay(artifact.display_data);
  const actions = useWorkbenchActions();
  const title = display?.title ?? "子 Agent";
  const role = artifact.agent_role ?? display?.role ?? null;
  const childTaskId = artifact.child_task_id ?? display?.childTaskId;
  // 后端把子 Task 最新 Run 的状态实时投影进 display.status；挂起（等审批 / 等消息）仍可停止。
  const status = display?.status ?? artifact.backendStatus;
  const isActive = status === "pending" || status === "running" || status === "suspended";
  const canOpen = childTaskId !== undefined && actions !== null && actions.workspaceId !== null;
  const canCancel = childTaskId !== undefined
    && isActive
    && interactive
    && !taskCancelling;
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
      actions?.openAgent({ taskId: childTaskId, title });
    }
  };
  const requestCancellation = async () => {
    if (!canCancel || childTaskId === undefined || cancellationPending) return;
    setCancellationPending(true);
    setCancellationError(null);
    void frontendLog("INFO", "delegation_child_run_cancel_requested", "用户请求停止子 Agent", {
      data: { child_task_id: childTaskId },
    });

    const result = await cancelTask(childTaskId);
    if (result.accepted) {
      // 停止信号只投递；子 Task 的终态经父委派工具 part 的 canonical 投影到达，不在这里乐观改状态。
      void frontendLog("INFO", "delegation_child_run_cancel_signalled", "子 Agent 停止信号已发出", {
        data: { child_task_id: childTaskId },
      });
      return;
    }

    setCancellationPending(false);
    setCancellationError(result.message);
    void frontendLog("WARNING", "delegation_child_run_cancel_failed", "停止子 Agent 请求失败", {
      data: {
        child_task_id: childTaskId,
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
  const meta = role ? <span className="text-muted-foreground max-w-[55%] truncate">{role}</span> : undefined;

  return (
    <Collapsible open={isOpen} onOpenChange={setOpen} className="group/tool-call terminal-surface overflow-hidden rounded-xl border shadow-sm">
      <div className={DARK_TOOL_CARD_HEADER_CLASS}>
        <DisclosureRow
          leading={<UsersIcon className="text-muted-foreground size-3.5" aria-hidden="true" />}
          label={<span className="truncate text-sm font-medium" title={title}>{title}</span>}
          meta={meta}
          trailing={status === "suspended" ? <SuspendedStatus /> : <ToolStatus status={status} />}
          tone="terminal"
          className="w-auto min-w-0 flex-1 border-b-0 bg-transparent px-3"
          data-testid="tool-activity-row"
          aria-label={`展开子 Agent详情：${title}`}
        />
        {canOpen && (
          <Button type="button" variant="ghost" size="sm" className="text-muted-foreground hover:bg-accent hover:text-foreground h-7 shrink-0 px-2" aria-label={`打开子 Agent工作台：${title}`} onClick={open}>
            打开
          </Button>
        )}
        {cancelAction}
      </div>
      <CollapsibleContent className={DARK_TOOL_CARD_CONTENT_CLASS}>
        <div className="text-foreground/80 border-border/60 space-y-1.5 border-t px-3 py-2 font-mono text-xs">
          {status === "completed" && <p className="text-muted-foreground">子 Agent 已完成；打开工作台查看完整结果。</p>}
          {status === "failed" && <p className="text-destructive">子 Agent 执行失败。</p>}
          {status === "cancelled" && <p className="text-muted-foreground">子 Agent 已取消。</p>}
          {cancellationError && <p className="text-warning" role="alert">{cancellationError}</p>}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

/** 子 Run 挂起没有对应的工具状态，文案沿用 Run 状态的统一呈现。 */
function SuspendedStatus() {
  return (
    <span className="text-warning inline-flex items-center gap-1.5 text-xs">
      <CirclePauseIcon className="size-3.5" aria-hidden="true" />
      {taskRunStatusPresentation("suspended")?.label}
    </span>
  );
}
