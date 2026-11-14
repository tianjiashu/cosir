import { useNavigate } from "react-router-dom";

import { AWAITING_USER_LABEL, executionStatusPresentation } from "@/components/session-status/session-status-indicator";
import { cancelSession } from "@/lib/assistant/session-commands";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useSessionAwaitingUserCalls, useSessionExecutionStatus } from "@/lib/session-status/session-status-store";
import { cn } from "@/lib/utils";
import { useWorkbenchActions } from "@/lib/workbench/workbench-context";
import { useOpenSession } from "@/components/links/use-open-session";
import { CallOpenButton } from "../call-open-button";
import type { CallView, CallViewContext } from "../call-view";
import { DetailSurface } from "./detail-parts";
import { readAgentSessionResultDisplay, readDelegationDisplay, readForkDisplay, type AgentSessionResultDisplay } from "../child-agent-display";
import { ToolStopButton, useStopRequest } from "../tool-stop-button";

/**
 * 子会话自身的异常（失败、已停止、等待结果）或在等你操作：派生调用当场完成，`completed` 只表示子会话已建好、任务说明已投递，
 * 子会话做得怎样只有这里知道（结论另以汇报消息到达）。
 * 运行中由调用行标题的扫光表达，完成是常态，都不说话。读会话状态推送而不是展示数据：展示数据是调用那一刻
 * 的快照，子 Run 之后还会变；推送还没到（null）时不显示，不猜。
 */
function ChildSessionProblem({ childSessionId }: { childSessionId: number | undefined; }) {
  const presentation = executionStatusPresentation(useSessionExecutionStatus(childSessionId));
  const awaitingUser = useSessionAwaitingUserCalls(childSessionId) > 0;
  if (awaitingUser) return <span className="text-info">{AWAITING_USER_LABEL}</span>;
  if (!presentation || presentation.active) return null;
  return <span className={cn(presentation.tone === "destructive" && "text-destructive")}>{presentation.label}</span>;
}

/** 在工作台打开子 Agent。 */
function OpenChildAgent({ childSessionId, title }: { childSessionId: number | undefined; title: string; }) {
  const actions = useWorkbenchActions();
  if (!actions || childSessionId === undefined) return null;
  return (
    <CallOpenButton
      label={`打开子 Agent 工作台：${title}`}
      onClick={() => {
        void frontendLog("INFO", "workbench_agent_open_requested", "主 Thread 请求打开子 Agent Workbench", { data: { childSessionId } });
        actions.openAgent({ sessionId: childSessionId, title });
      }}
    />
  );
}

/** 跳到分叉会话的侧栏路由。 */
function OpenFork({ childSessionId, title }: { childSessionId: number; title: string; }) {
  const navigate = useNavigate();
  return (
    <CallOpenButton
      label={`打开分叉会话：${title}`}
      onClick={() => {
        void frontendLog("INFO", "fork_session_open_requested", "主 Thread 请求打开分叉会话", { data: { childSessionId } });
        navigate(`/sessions/${childSessionId}`);
      }}
    />
  );
}

/**
 * 停止派生出的子会话：委派、分叉与状态查询行共用。按 Session 停止（用户停止只打断它当前的运行），子会话
 * 活动时才出现；停止信号只投递，终态经会话状态推送到达，不乐观改状态。
 */
function ChildStopAction({ childSessionId, title, context }: { childSessionId: number; title: string; context: CallViewContext; }) {
  const status = useSessionExecutionStatus(childSessionId);
  const active = status === "running" || status === "suspended";
  const cancellation = useStopRequest(childSessionId, "session");
  if (!active || !context.interactive || context.sessionCancelling) return null;
  const stop = () => {
    void frontendLog("INFO", "delegation_child_run_cancel_requested", "用户请求停止派生的 Session", { data: { child_session_id: childSessionId } });
    void cancellation.stop(async () => {
      const result = await cancelSession(childSessionId);
      if (result.accepted) {
        void frontendLog("INFO", "delegation_child_run_cancel_signalled", "派生 Session 停止信号已发出", { data: { child_session_id: childSessionId } });
        return null;
      }
      void frontendLog("WARNING", "delegation_child_run_cancel_failed", "停止派生 Session 请求失败", { data: { child_session_id: childSessionId, reason: result.reason } });
      return result.message;
    });
  };
  return (
    <>
      {cancellation.error && <span className="text-warning text-xs" role="alert">{cancellation.error}</span>}
      <ToolStopButton label={`会话：${title}`} pending={cancellation.pending} onClick={stop} />
    </>
  );
}

function childSessionIdOf(context: CallViewContext, fromDisplay: number | undefined): number | undefined {
  return context.artifact.childSessionId ?? fromDisplay;
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
    status: context.inherited ? null : <ChildSessionProblem childSessionId={childSessionIdOf(context, display.childSessionId)} />,
    actions: context.inherited ? null : <DelegationActions childSessionId={childSessionIdOf(context, display.childSessionId)} title={rowTitle(context, display.title)} context={context} />,
  };
};

function DelegationActions({ childSessionId, title, context }: { childSessionId: number | undefined; title: string; context: CallViewContext; }) {
  return (
    <>
      <OpenChildAgent childSessionId={childSessionId} title={title} />
      {childSessionId !== undefined && <ChildStopAction childSessionId={childSessionId} title={title} context={context} />}
    </>
  );
}

/** 打开对方（按 Session 类型跳路由或进工作台）；只有状态查询可以停止所管理的子会话。 */
function AgentSessionActions({ display, context }: { display: AgentSessionResultDisplay; context: CallViewContext; }) {
  const openSession = useOpenSession();
  const title = display.agentName ?? "会话";
  return (
    <>
      <CallOpenButton label={`打开会话：${title}`} onClick={() => void openSession(display.sessionId, title)} />
      {display.operation === "status" && <ChildStopAction childSessionId={display.sessionId} title={title} context={context} />}
    </>
  );
}

/** `agent-session-result`（`send_message` / `session_status`）：展开体是对方返回的结论正文（状态查询时才有）。 */
export const agentSessionResultView: CallView = (data, context) => {
  const result = readAgentSessionResultDisplay(data);
  if (!result) return null;
  const live = result.operation === "status" && !context.inherited;
  return {
    meta: live ? undefined : result.agentName,
    status: live ? <ChildSessionProblem childSessionId={result.sessionId} /> : undefined,
    detail: result.finalOutput || result.endReason ? {
      render: () => (
        <DetailSurface className="space-y-1">
          {result.endReason && <p className="text-warning">{result.endReason}</p>}
          {result.finalOutput && <p className="text-muted-foreground break-words whitespace-pre-wrap">{result.finalOutput}</p>}
        </DetailSurface>
      )
    } : undefined,
    actions: context.inherited ? null : <AgentSessionActions display={result} context={context} />,
  };
};

/** 分叉指令保留空字符串可展开的既有语义。 */
export const forkSessionView: CallView = (data, context) => {
  const display = readForkDisplay(data);
  if (!display) return null;
  const childSessionId = childSessionIdOf(context, display.childSessionId)!;
  return {
    detail: typeof context.args.message === "string" ? { render: () => <DetailSurface className="text-muted-foreground whitespace-pre-wrap break-words">{context.args.message as string}</DetailSurface> } : undefined,
    status: context.inherited ? null : <ChildSessionProblem childSessionId={childSessionId} />,
    actions: context.inherited ? null : (
      <>
        <OpenFork childSessionId={childSessionId} title={display.title} />
        <ChildStopAction childSessionId={childSessionId} title={display.title} context={context} />
      </>
    ),
  };
};
