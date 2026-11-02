import { ArrowUpRightIcon } from "lucide-react";
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

import { taskRunStatusPresentation } from "@/components/task-tree/task-run-status";
import { Button } from "@/components/ui/button";
import { cancelTask } from "@/lib/assistant/task-commands";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useTaskExecutionStatus } from "@/lib/task-status/task-status-store";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { useEnsureTaskListed } from "@/components/layout/task-listing-context";
import { cn } from "@/lib/utils";
import type { CallView, CallViewContext } from "../call-view";
import { readChildAgentResultDisplay, readDelegationDisplay, readForkDisplay } from "../child-agent-display";
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
 * 行尾动作：打开派生出的 Task；活动中的还可以单独停止它（停止信号只投递，终态经任务状态推送与
 * 父工具 part 的 canonical 投影到达，不乐观改状态）。怎么「打开」由视图给：委派在工作台打开，
 * 分叉是侧栏对话、跳到它的路由。
 */
function ChildTaskActions({ childTaskId, title, context, open, openLabel, subject }: {
  childTaskId: number | undefined;
  title: string;
  context: CallViewContext;
  open: (() => void) | null;
  openLabel: string;
  /** 停止按钮里怎么称呼它（「子 Agent」「分叉对话」）。 */
  subject: string;
}) {
  const status = useTaskExecutionStatus(childTaskId);
  const active = status === "pending" || status === "running" || status === "suspended";
  const cancellation = useStopRequest(active);
  if (childTaskId === undefined) return null;
  const canCancel = active && context.interactive && !context.taskCancelling;
  const stop = () => {
    void frontendLog("INFO", "delegation_child_run_cancel_requested", "用户请求停止派生的 Task", { data: { child_task_id: childTaskId } });
    void cancellation.stop(async () => {
      const result = await cancelTask(childTaskId);
      if (result.accepted) {
        void frontendLog("INFO", "delegation_child_run_cancel_signalled", "派生 Task 停止信号已发出", { data: { child_task_id: childTaskId } });
        return null;
      }
      void frontendLog("WARNING", "delegation_child_run_cancel_failed", "停止派生 Task 请求失败", { data: { child_task_id: childTaskId, reason: result.reason } });
      return result.message;
    });
  };
  return (
    <>
      {cancellation.error && <span className="text-warning text-xs" role="alert">{cancellation.error}</span>}
      {open && (
        <Button type="button" variant="ghost" size="xs" className="text-muted-foreground hover:text-foreground shrink-0" onClick={open} aria-label={`${openLabel}：${title}`}>
          打开<ArrowUpRightIcon aria-hidden="true" />
        </Button>
      )}
      {canCancel && <ToolStopButton label={`${subject}：${title}`} pending={cancellation.pending} onClick={stop} />}
    </>
  );
}

/** 在工作台打开子 Agent（委派、追加消息、状态查询的卡片）。 */
function WorkbenchChildActions({ childTaskId, title, context }: { childTaskId: number | undefined; title: string; context: CallViewContext }) {
  const actions = useWorkbenchActions();
  const open = actions && childTaskId !== undefined
    ? () => {
      void frontendLog("INFO", "workbench_agent_open_requested", "主 Thread 请求打开子 Agent Workbench", { data: { childTaskId } });
      actions.openAgent({ taskId: childTaskId, title });
    }
    : null;
  return <ChildTaskActions childTaskId={childTaskId} title={title} context={context} open={open} openLabel="打开子 Agent 工作台" subject="子 Agent" />;
}

/** 分叉对话的卡片：跳到它的侧栏路由；卡片出现时侧栏里还没有它就补刷一次目录。 */
function ForkActions({ childTaskId, title, context }: { childTaskId: number; title: string; context: CallViewContext }) {
  const navigate = useNavigate();
  const ensureTaskListed = useEnsureTaskListed();
  useEffect(() => {
    ensureTaskListed?.(childTaskId);
  }, [childTaskId, ensureTaskListed]);
  const open = () => {
    void frontendLog("INFO", "fork_task_open_requested", "主 Thread 请求打开分叉对话", { data: { childTaskId } });
    navigate(`/tasks/${childTaskId}`);
  };
  return <ChildTaskActions childTaskId={childTaskId} title={title} context={context} open={open} openLabel="打开分叉对话" subject="分叉对话" />;
}

function childTaskIdOf(context: CallViewContext, fromDisplay: number | undefined): number | undefined {
  return context.artifact.childTaskId ?? fromDisplay;
}

/** 子 Agent 的名字：展示数据里的优先，排队时参数里的 `agent_name` 也行。 */
function rowTitle(context: CallViewContext, fromDisplay: string | undefined): string {
  const fromArgs = typeof context.args.agent_name === "string" && context.args.agent_name.trim() ? context.args.agent_name : undefined;
  return fromDisplay ?? fromArgs ?? "子 Agent";
}

/** `delegation-result`（`delegate_task`）：没有展开体，子 Agent 的过程与结论都在工作台里。 */
export const delegationView: CallView = {
  accepts: (data) => readDelegationDisplay(data) !== null,
  meta: (data, context) => <ChildTaskStatus childTaskId={childTaskIdOf(context, readDelegationDisplay(data)?.childTaskId)} />,
  actions: (data, context) => {
    const display = readDelegationDisplay(data);
    return <WorkbenchChildActions childTaskId={childTaskIdOf(context, display?.childTaskId)} title={rowTitle(context, display?.title)} context={context} />;
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
    return <WorkbenchChildActions childTaskId={childTaskIdOf(context, result.childTaskId)} title={rowTitle(context, result.agentName)} context={context} />;
  },
};

/** `fork-task-result`（`fork_task`）：没有展开体，分叉对话的过程与结论都在它自己的侧栏对话里。 */
export const forkTaskView: CallView = {
  accepts: (data) => readForkDisplay(data) !== null,
  meta: (data, context) => <ChildTaskStatus childTaskId={childTaskIdOf(context, readForkDisplay(data)!.childTaskId)} />,
  actions: (data, context) => {
    const display = readForkDisplay(data)!;
    return <ForkActions childTaskId={childTaskIdOf(context, display.childTaskId)!} title={display.title} context={context} />;
  },
};

