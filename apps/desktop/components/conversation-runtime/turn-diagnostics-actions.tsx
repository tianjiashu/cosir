import { ClipboardListIcon, ScanSearchIcon } from "lucide-react";
import type { ReactElement } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { copyText } from "@/lib/clipboard";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { isTerminalRunStatus } from "@/lib/assistant/contract";

export type TurnDiagnosticsActionsProps = {
  /** 当前会话所属的 Session 标识，由 Thread 页面上下文提供。 */
  sessionId: number;
  /** 这条回复所在的回合：诊断与会话观测都按回合进入，前端看不到 Run。 */
  turn: number;
  /** 继承来的回合：只能查看上下文，没有本地运行的结局可复制。 */
  inherited: boolean;
  /** 所属运行的结局状态：回复上的 `outcome.status`，当前回复取 `activity.status`。 */
  status: string | null;
  endReason?: string | null;
  errorCode?: string | null;
};

type TurnDiagnosticsInput = {
  sessionId: number;
  turn: number;
  status: string;
  endReason?: string | null;
  errorCode?: string | null;
};

/**
 * 构造可直接粘贴到问题反馈里的诊断文本。
 *
 * 采用 `key=value` 逐行格式，缺失的可选字段整行省略。后端日志按 session_id 记录；按回合排查时，在上下文
 * 观测页查看这一回合所属运行的状态与起止时间，再按 session_id 与时间窗检索日志。
 */
export function formatTurnDiagnostics({ sessionId, turn, status, endReason, errorCode }: TurnDiagnosticsInput): string {
  const lines = [`session_id=${sessionId}`, `turn=${turn}`, `run_status=${status}`];
  if (endReason) lines.push(`end_reason=${endReason}`);
  if (errorCode) lines.push(`error_code=${errorCode}`);
  return lines.join("\n");
}

/**
 * 回复操作栏里的诊断入口：复制诊断信息、跳转会话观测。
 *
 * 本地回复只在它带着运行的终态结局时提供（中间态不是最终诊断信息）；继承来的回复只能跳转观测、查看
 * 那一回合的上下文。本组件不请求后端，跳转时携带来源路由供观测页返回。
 */
export function TurnDiagnosticsActions({ sessionId, turn, inherited, status, endReason, errorCode }: TurnDiagnosticsActionsProps): ReactElement | null {
  const navigate = useNavigate();
  const location = useLocation();
  const terminal = status !== null && isTerminalRunStatus(status);
  if (!inherited && !terminal) return null;

  const openObserver = () => {
    const query = new URLSearchParams({ session_id: String(sessionId), turn: String(turn) }).toString();
    navigate(`/session-observer?${query}`, { state: { returnTo: `${location.pathname}${location.search}` } });
  };
  const observer = (
    <TooltipIconButton
      tooltip="在会话观测中查看本回合"
      aria-label="在会话观测中查看本回合"
      size="sm"
      data-testid="turn-diagnostics-observer"
      onClick={openObserver}
    >
      <ScanSearchIcon />
    </TooltipIconButton>
  );
  if (inherited || status === null) return observer;

  const copyDiagnostics = () => {
    void copyText(formatTurnDiagnostics({ sessionId, turn, status, endReason, errorCode }), {
      success: "已复制诊断信息",
      description: `会话 ${sessionId} · 回合 ${turn}`,
      failure: "复制诊断信息失败",
      logEvent: "turn_diagnostics_copy_failed",
      logData: { sessionId, turn },
    });
  };

  return (
    <>
      <TooltipIconButton
        tooltip="复制诊断信息"
        aria-label="复制诊断信息"
        size="sm"
        data-testid="turn-diagnostics-copy"
        onClick={copyDiagnostics}
      >
        <ClipboardListIcon />
      </TooltipIconButton>
      {observer}
    </>
  );
}
