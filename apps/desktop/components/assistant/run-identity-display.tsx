"use client";

import { CheckIcon, CopyIcon } from "lucide-react";
import type { ReactElement } from "react";
import { Link, useLocation } from "react-router-dom";

import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard";
import { isTerminalRunStatus } from "@/lib/assistant/contract";

export type RunIdentityDisplayProps = {
  /** 当前对话所属的 Task 标识，由 Thread 页面上下文提供。 */
  taskId: number | null | undefined;
  /** 当前 Assistant 消息所属的 Run 标识。 */
  runId: number | null;
  /** 当前 Run 的后端状态。 */
  status: string | null;
  /** 仅在当前 Run 的最后一条消息上显示。 */
  visible: boolean;
};

/** 构造可粘贴到日志或问题反馈中的稳定 Task/Run 标识文本。 */
export function formatRunIdentity(taskId: number, runId: number): string {
  return `task_id=${taskId}\nrun_id=${runId}`;
}

/** 判断 Task/Run 标识是否应在当前消息下展示。 */
export function shouldDisplayRunIdentity({
  taskId,
  runId,
  status,
  visible,
}: RunIdentityDisplayProps): boolean {
  return visible
    && taskId != null
    && runId != null
    && isTerminalRunStatus(status);
}

/**
 * 在 Run 终态消息下展示 Task/Run 标识，并分别提供复制和观测页导航。
 *
 * Task/Run 标识分别导航到对应查询范围，并携带来源路由供观测页返回；复制图标保持独立。
 * 本组件不读取数据库、不请求后端，也不维护 Run 状态。
 * Run 尚未进入 completed、failed 或 cancelled 时不显示，避免把中间态误认为最终诊断信息。
 */
export function RunIdentityDisplay(props: RunIdentityDisplayProps): ReactElement | null {
  const { taskId, runId } = props;
  const location = useLocation();
  const { isCopied, copyToClipboard } = useCopyToClipboard();

  if (!shouldDisplayRunIdentity(props) || taskId == null || runId == null) return null;

  const identity = formatRunIdentity(taskId, runId);
  const taskObserverQuery = new URLSearchParams({ task_id: String(taskId) }).toString();
  const runObserverQuery = new URLSearchParams({
    task_id: String(taskId),
    run_id: String(runId),
  }).toString();
  const returnTo = `${location.pathname}${location.search}`;
  const linkClassName = "rounded-sm transition-colors hover:text-foreground hover:underline hover:underline-offset-2 focus-visible:text-foreground focus-visible:underline focus-visible:underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";
  return (
    <div data-testid="run-identity-display" className="text-muted-foreground mt-2 flex items-center gap-2 text-xs">
      <Link to={`/context-observer?${taskObserverQuery}`} state={{ returnTo }} aria-label={`在上下文观测中查看 Task ${taskId} 的全部消息`} data-testid="run-identity-task-link" className={linkClassName}>task_id={taskId}</Link>
      <span aria-hidden="true">·</span>
      <Link to={`/context-observer?${runObserverQuery}`} state={{ returnTo }} aria-label={`在上下文观测中查看 Task ${taskId}、Run ${runId}`} data-testid="run-identity-run-link" className={linkClassName}>run_id={runId}</Link>
      <TooltipIconButton
        tooltip={isCopied ? "已复制" : "复制 Task/Run 标识"}
        aria-label={isCopied ? "已复制 Task/Run 标识" : "复制 Task/Run 标识"}
        size="sm"
        onClick={() => copyToClipboard(identity)}
      >
        {isCopied ? <CheckIcon /> : <CopyIcon />}
      </TooltipIconButton>
    </div>
  );
}
