import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

import { taskRunStatusPresentation } from "@/components/task-tree/task-run-status";
import { cancelTask } from "@/lib/assistant/task-commands";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useTaskExecutionStatus } from "@/lib/task-status/task-status-store";
import { cn } from "@/lib/utils";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { useEnsureTaskListed } from "@/components/layout/task-listing-context";
import { useOpenTask } from "@/components/links/use-open-task";
import { CallOpenButton } from "../call-open-button";
import type { CallView, CallViewContext } from "../call-view";
import { DetailSurface } from "../details/detail-view";
import { readAgentTaskResultDisplay, readDelegationDisplay, readForkDisplay, type AgentTaskResultDisplay } from "../child-agent-display";
import { ToolStopButton, useStopRequest } from "../tool-stop-button";

/**
 * 子任务自身的异常（失败、已停止、等待中）：工具 `completed` 只表示结论已交付，子任务是否成功只有这里知道。
 * 运行中由调用行标题的扫光表达，完成是常态，都不说话。读任务状态推送而不是展示数据：展示数据是调用那一刻
 * 的快照，子 Run 之后还会变；推送还没到（null）时不显示，不猜。
 */
function ChildTaskProblem({ childTaskId }: { childTaskId: number | undefined; }) {
  const presentation = taskRunStatusPresentation(useTaskExecutionStatus(childTaskId));
  if (!presentation || presentation.active) return null;
  return <span className={cn(presentation.tone === "destructive" && "text-destructive")}>{presentation.label}</span>;
}

/**
 * 分叉卡片的状态节点。卡片出现时侧栏目录里可能还没有它（Agent 在父 Run 运行中建出），在这里补刷一次：
 * 时间线的后台行不挂操作，状态是行上唯一常驻的节点。
 */
function ForkStatus({ childTaskId }: { childTaskId: number; }) {
  const ensureTaskListed = useEnsureTaskListed();
  useEffect(() => {
    ensureTaskListed?.(childTaskId);
  }, [childTaskId, ensureTaskListed]);
  return <ChildTaskProblem childTaskId={childTaskId} />;
}

/** 在工作台打开子 Agent。 */
function OpenChildAgent({ childTaskId, title }: { childTaskId: number | undefined; title: string; }) {
  const actions = useWorkbenchActions();
  if (!actions || childTaskId === undefined) return null;
  return (
    <CallOpenButton
      label={`打开子 Agent 工作台：${title}`}
      onClick={() => {
        void frontendLog("INFO", "workbench_agent_open_requested", "主 Thread 请求打开子 Agent Workbench", { data: { childTaskId } });
        actions.openAgent({ taskId: childTaskId, title });
      }}
    />
  );
}

/** 跳到分叉对话的侧栏路由。 */
function OpenFork({ childTaskId, title }: { childTaskId: number; title: string; }) {
  const navigate = useNavigate();
  return (
    <CallOpenButton
      label={`打开分叉对话：${title}`}
      onClick={() => {
        void frontendLog("INFO", "fork_task_open_requested", "主 Thread 请求打开分叉对话", { data: { childTaskId } });
        navigate(`/tasks/${childTaskId}`);
      }}
    />
  );
}

/**
 * 状态查询行的停止：委派与分叉的停止在后台卡片上（`cancelToolCall`），已结束的条目没有停止，子任务被
 * 消息重新唤醒后只有这里能单独停它。停止信号只投递，终态经任务状态推送到达，不乐观改状态。
 */
function ChildStopAction({ childTaskId, title, context }: { childTaskId: number; title: string; context: CallViewContext; }) {
  const status = useTaskExecutionStatus(childTaskId);
  const active = status === "pending" || status === "running" || status === "suspended";
  const cancellation = useStopRequest(childTaskId, "task");
  if (!active || !context.interactive || context.taskCancelling) return null;
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
      <ToolStopButton label={`任务：${title}`} pending={cancellation.pending} onClick={stop} />
    </>
  );
}

function childTaskIdOf(context: CallViewContext, fromDisplay: number | undefined): number | undefined {
  return context.artifact.childTaskId ?? fromDisplay;
}

/** 子 Agent 的名字：展示数据里的优先，排队时参数里的 `agent_name` 也行。 */
function rowTitle(context: CallViewContext, fromDisplay: string | undefined): string {
  const fromArgs = typeof context.args.agent_name === "string" && context.args.agent_name.trim() ? context.args.agent_name : undefined;
  return fromDisplay ?? fromArgs ?? "子 Agent";
}

/** `delegation-result`（`delegate_task`）：展开体只给委派指令，子 Agent 的过程与结论在工作台里。 */
export const delegationView: CallView = (data, context) => {
  const display = readDelegationDisplay(data);
  if (!display) return null;
  return {
    detail: typeof context.args.message === "string" ? { render: () => <DetailSurface className="text-muted-foreground whitespace-pre-wrap break-words">{context.args.message as string}</DetailSurface> } : undefined,
    status: context.inherited ? null : <ChildTaskProblem childTaskId={childTaskIdOf(context, display.childTaskId)} />,
    actions: context.inherited ? null : <OpenChildAgent childTaskId={childTaskIdOf(context, display.childTaskId)} title={rowTitle(context, display.title)} />,
  };
};

/** 打开对方（按 Task 类型跳路由或进工作台）；只有状态查询可以停止所管理的子任务。 */
function AgentTaskActions({ display, context }: { display: AgentTaskResultDisplay; context: CallViewContext; }) {
  const openTask = useOpenTask();
  const title = display.agentName ?? "任务";
  return (
    <>
      <CallOpenButton label={`打开任务：${title}`} onClick={() => void openTask(display.taskId, title)} />
      {display.operation === "status" && <ChildStopAction childTaskId={display.taskId} title={title} context={context} />}
    </>
  );
}

/** `agent-task-result`（`send_message` / `task_status`）：展开体是对方返回的结论正文（状态查询时才有）。 */
export const agentTaskResultView: CallView = (data, context) => {
  const result = readAgentTaskResultDisplay(data);
  if (!result) return null;
  const live = result.operation === "status" && !context.inherited;
  return {
    meta: live ? undefined : result.agentName,
    status: live ? <ChildTaskProblem childTaskId={result.taskId} /> : undefined,
    detail: result.finalOutput || result.endReason ? {
      render: () => (
        <DetailSurface className="space-y-1">
          {result.endReason && <p className="text-warning">{result.endReason}</p>}
          {result.finalOutput && <p className="text-muted-foreground break-words whitespace-pre-wrap">{result.finalOutput}</p>}
        </DetailSurface>
      )
    } : undefined,
    actions: context.inherited ? null : <AgentTaskActions display={result} context={context} />,
  };
};

/** 分叉指令保留空字符串可展开的既有语义。 */
export const forkTaskView: CallView = (data, context) => {
  const display = readForkDisplay(data);
  if (!display) return null;
  const childTaskId = childTaskIdOf(context, display.childTaskId)!;
  return {
    detail: typeof context.args.message === "string" ? { render: () => <DetailSurface className="text-muted-foreground whitespace-pre-wrap break-words">{context.args.message as string}</DetailSurface> } : undefined,
    status: context.inherited ? null : <ForkStatus childTaskId={childTaskId} />,
    actions: context.inherited ? null : <OpenFork childTaskId={childTaskId} title={display.title} />,
  };
};
