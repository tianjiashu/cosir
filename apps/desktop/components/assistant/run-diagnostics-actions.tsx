"use client";

import { ClipboardListIcon, ScanSearchIcon } from "lucide-react";
import type { ReactElement } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuiState } from "@assistant-ui/react";

import { TooltipIconButton } from "@/components/tooltip-icon-button";
import { isTerminalRunStatus, type TransportRun } from "@/lib/assistant/contract";
import { findTransportRun } from "@/lib/assistant/transport-state-operations";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

export type RunDiagnosticsActionsProps = {
  /** 当前对话所属的 Task 标识，由 Thread 页面上下文提供。 */
  taskId: number | null | undefined;
  /** 当前 Assistant 消息所属的 Run 标识。 */
  runId: number | null;
  /** 当前 Run 的后端状态（随消息 metadata 投影）。 */
  status: string | null;
  /** 仅在当前 Run 的最后一条消息上提供。 */
  visible: boolean;
};

type RunDiagnosticsInput = {
  taskId: number;
  runId: number;
  status: string;
  /** 只读作用域没有 Transport external state，此时为 null，只输出标识与状态。 */
  run: Pick<TransportRun, "endReason" | "langfuseTraceId" | "error"> | null;
};

/**
 * 构造可直接粘贴到问题反馈或日志检索中的诊断文本。
 *
 * 采用 `key=value` 逐行格式：字段名与后端结构化日志 data 中的键一致，
 * 便于在 backend 日志里按 task_id / run_id 直接 grep；缺失的可选字段整行省略。
 */
export function formatRunDiagnostics({ taskId, runId, status, run }: RunDiagnosticsInput): string {
  const lines = [`task_id=${taskId}`, `run_id=${runId}`, `run_status=${status}`];
  if (run?.endReason) lines.push(`end_reason=${run.endReason}`);
  if (run?.error) lines.push(`error_code=${run.error.code}`);
  if (run?.langfuseTraceId) lines.push(`langfuse_trace_id=${run.langfuseTraceId}`);
  return lines.join("\n");
}

/** 诊断操作只在 Run 终态的最后一条消息上提供，避免把中间态误当成最终诊断信息。 */
export function shouldOfferRunDiagnostics({
  taskId,
  runId,
  status,
  visible,
}: RunDiagnosticsActionsProps): boolean {
  return visible
    && taskId != null
    && runId != null
    && isTerminalRunStatus(status);
}

/**
 * 消息操作栏里的 Run 诊断入口：复制诊断信息、跳转上下文观测。
 *
 * 开发者标识不再常驻在阅读流里，只在需要排查时从操作栏取用。
 * 本组件不请求后端，也不维护 Run 状态；跳转时携带来源路由供观测页返回。
 */
export function RunDiagnosticsActions(props: RunDiagnosticsActionsProps): ReactElement | null {
  const { taskId, runId, status } = props;
  const run = useAuiState((state) => findTransportRun(state.thread.state, runId) ?? null);
  const navigate = useNavigate();
  const location = useLocation();

  if (!shouldOfferRunDiagnostics(props) || taskId == null || runId == null || status === null) return null;

  const copyDiagnostics = () => {
    const text = formatRunDiagnostics({ taskId, runId, status, run });
    // 经 Promise 链调用：环境缺少 clipboard API 时的同步 TypeError 也落到失败分支。
    void Promise.resolve()
      .then(() => navigator.clipboard.writeText(text))
      .then(
        () => notify.success("已复制诊断信息", { description: `任务 ${taskId} · 轮次 ${runId}` }),
        (error: unknown) => {
          notify.error("复制诊断信息失败", { error, fallback: "无法写入剪贴板" });
          void frontendLog("WARNING", "run_diagnostics_copy_failed", "复制 Run 诊断信息失败", {
            data: { taskId, runId },
            error,
          });
        },
      );
  };

  const openObserver = () => {
    const query = new URLSearchParams({ task_id: String(taskId), run_id: String(runId) }).toString();
    navigate(`/context-observer?${query}`, { state: { returnTo: `${location.pathname}${location.search}` } });
  };

  return (
    <>
      <TooltipIconButton
        tooltip="复制诊断信息"
        aria-label="复制诊断信息"
        size="sm"
        data-testid="run-diagnostics-copy"
        onClick={copyDiagnostics}
      >
        <ClipboardListIcon />
      </TooltipIconButton>
      <TooltipIconButton
        tooltip="在上下文观测中查看本轮"
        aria-label="在上下文观测中查看本轮"
        size="sm"
        data-testid="run-diagnostics-observer"
        onClick={openObserver}
      >
        <ScanSearchIcon />
      </TooltipIconButton>
    </>
  );
}
