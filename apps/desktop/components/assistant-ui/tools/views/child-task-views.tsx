import { ArrowUpRightIcon } from "lucide-react";

import { taskRunStatusPresentation } from "@/components/task-tree/task-run-status";
import { Button } from "@/components/ui/button";
import { cancelTask } from "@/lib/assistant/task-commands";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useTaskExecutionStatus } from "@/lib/task-status/task-status-store";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { cn } from "@/lib/utils";
import type { CallView, CallViewContext } from "../call-view";
import { readChildAgentResultDisplay, readDelegationDisplay } from "../child-agent-display";
import { ToolStopButton, useStopRequest } from "../tool-stop-button";

/**
 * 子任务状态只读任务状态推送（`useTaskExecutionStatus`），不读展示数据：展示数据是调用那一刻的快照，
 * 子 Run 之后还会变。推送还没到（null）时不显示，不猜。
 */
function ChildTaskStatus({ childTaskId }: { childTaskId: number | undefined }) {
  const status = useTaskExecutionStatus(childTaskId);
  const presentation = taskRunStatusPresentation(status, true);
  if (!presentation) return null;
  return <span className={cn(presentation.active && "text-info")}>{presentation.label}</span>;
}

/**
 * 行尾动作：打开子任务工作台；活动中的委派还可以单独停止子 Agent（停止信号只投递，终态经任务状态推送与
 * 父工具 part 的 canonical 投影到达，不乐观改状态）。
 */
function ChildTaskActions({ childTaskId, title, context }: { childTaskId: number | undefined; title: string; context: CallViewContext }) {
  const actions = useWorkbenchActions();
  const status = useTaskExecutionStatus(childTaskId);
  const active = status === "pending" || status === "running" || status === "suspended";
  const cancellation = useStopRequest(active);
  if (childTaskId === undefined) return null;
  const canOpen = actions !== null;
  const canCancel = active && context.interactive && !context.taskCancelling;
  const open = () => {
    void frontendLog("INFO", "workbench_agent_open_requested", "主 Thread 请求打开子 Agent Workbench", { data: { childTaskId } });
    actions?.openAgent({ taskId: childTaskId, title });
  };
  const stop = () => {
    void frontendLog("INFO", "delegation_child_run_cancel_requested", "用户请求停止子 Agent", { data: { child_task_id: childTaskId } });
    void cancellation.stop(async () => {
      const result = await cancelTask(childTaskId);
      if (result.accepted) {
        void frontendLog("INFO", "delegation_child_run_cancel_signalled", "子 Agent 停止信号已发出", { data: { child_task_id: childTaskId } });
        return null;
      }
      void frontendLog("WARNING", "delegation_child_run_cancel_failed", "停止子 Agent 请求失败", { data: { child_task_id: childTaskId, reason: result.reason } });
      return result.message;
    });
  };
  return (
    <>
      {cancellation.error && <span className="text-warning text-xs" role="alert">{cancellation.error}</span>}
      {canOpen && (
        <Button type="button" variant="ghost" size="xs" className="text-muted-foreground hover:text-foreground shrink-0" onClick={open} aria-label={`打开子 Agent 工作台：${title}`}>
          打开<ArrowUpRightIcon aria-hidden="true" />
        </Button>
      )}
      {canCancel && <ToolStopButton label={`子 Agent：${title}`} pending={cancellation.pending} onClick={stop} />}
    </>
  );
}

function childTaskIdOf(context: CallViewContext, fromDisplay: number | undefined): number | undefined {
  return context.artifact.childTaskId ?? fromDisplay;
}

function rowTitle(context: CallViewContext, fallback: string | undefined): string {
  return context.artifact.heading?.title ?? fallback ?? "子 Agent";
}

/** `delegation-result`（`delegate_task`）：没有展开体，子 Agent 的过程与结论都在工作台里。 */
export const delegationView: CallView = {
  accepts: (data) => readDelegationDisplay(data) !== null,
  meta: (data, context) => <ChildTaskStatus childTaskId={childTaskIdOf(context, readDelegationDisplay(data)?.childTaskId)} />,
  actions: (data, context) => {
    const display = readDelegationDisplay(data);
    return <ChildTaskActions childTaskId={childTaskIdOf(context, display?.childTaskId)} title={rowTitle(context, display?.title)} context={context} />;
  },
};

/** `child-agent-result`（`child_agent_send` / `child_agent_status`）：展开体是子 Agent 返回的结论正文。 */
export const childAgentResultView: CallView = {
  accepts: (data) => readChildAgentResultDisplay(data) !== null,
  meta: (data, context) => <ChildTaskStatus childTaskId={childTaskIdOf(context, readChildAgentResultDisplay(data)!.childTaskId)} />,
  body: (data) => {
    const result = readChildAgentResultDisplay(data)!;
    if (!result.finalOutput && !result.endReason) return null;
    return (
      <div className="bg-surface space-y-1 rounded-lg px-3 py-2 text-xs">
        {result.endReason && <p className="text-warning">{result.endReason}</p>}
        {result.finalOutput && <p className="text-muted-foreground break-words whitespace-pre-wrap">{result.finalOutput}</p>}
      </div>
    );
  },
  actions: (data, context) => {
    const result = readChildAgentResultDisplay(data)!;
    return <ChildTaskActions childTaskId={childTaskIdOf(context, result.childTaskId)} title={rowTitle(context, result.agentName)} context={context} />;
  },
};
