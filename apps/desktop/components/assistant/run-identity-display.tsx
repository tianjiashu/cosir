"use client";

import { CheckIcon, CopyIcon } from "lucide-react";
import type { ReactElement } from "react";

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
 * 在 Run 终态消息下展示 Task/Run 标识并提供复制能力。
 *
 * 本组件只负责 UI 投影和剪贴板交互，不读取数据库、不请求后端，也不维护 Run 状态。
 * Run 尚未进入 completed、failed 或 cancelled 时不显示，避免把中间态误认为最终诊断信息。
 */
export function RunIdentityDisplay(props: RunIdentityDisplayProps): ReactElement | null {
  const { taskId, runId } = props;
  const { isCopied, copyToClipboard } = useCopyToClipboard();

  if (!shouldDisplayRunIdentity(props) || taskId == null || runId == null) return null;

  const identity = formatRunIdentity(taskId, runId);
  return (
    <div className="text-muted-foreground mt-2 flex items-center gap-2 text-xs">
      <span data-testid="run-identity-display">
        Task ID: {taskId} · Run ID: {runId}
      </span>
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
