import type { BadgeTone } from "@/components/ui/badge";

/**
 * 终端会话生命周期状态（后端 terminal session 的 status 字段）→ 中文标签与语义色调的唯一映射表。
 *
 * 状态值是后端事实，这里只负责展示投影；未登记的取值返回 null，由调用方决定如何呈现
 * 并记录日志，不在此处静默兜底成某个已知状态。
 */
const TERMINAL_SESSION_STATUS_PRESENTATION = {
  starting: { label: "启动中", tone: "info" },
  running: { label: "运行中", tone: "success" },
  exited: { label: "已退出", tone: "neutral" },
  interrupted: { label: "已中断", tone: "warning" },
  failed: { label: "会话失败", tone: "destructive" },
  closed: { label: "已关闭", tone: "neutral" },
} as const satisfies Record<string, { label: string; tone: BadgeTone }>;

export type TerminalSessionStatus = keyof typeof TERMINAL_SESSION_STATUS_PRESENTATION;

export type TerminalSessionStatusPresentation = { label: string; tone: BadgeTone };

export function terminalSessionStatusPresentation(status: string): TerminalSessionStatusPresentation | null {
  return Object.hasOwn(TERMINAL_SESSION_STATUS_PRESENTATION, status)
    ? TERMINAL_SESSION_STATUS_PRESENTATION[status as TerminalSessionStatus]
    : null;
}
