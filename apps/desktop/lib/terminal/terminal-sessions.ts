import type { TerminalSessionEntry, TerminalSessionStatus } from "@/lib/assistant/contract";

/**
 * 终端会话的展示规则（纯函数）：生命周期分组、状态文案与标签命名。
 *
 * 会话事实只来自 snapshot 的 `terminal_sessions`，这里不做任何推断，只决定「怎么显示」；
 * 标签栏、页头角标与工具卡片共用同一套规则。
 */

export function isActiveTerminalSession(session: Pick<TerminalSessionEntry, "status">): boolean {
  return session.status === "starting" || session.status === "running";
}

/** `dot` / `text` 是 Tailwind 语义色类，标签状态点与工具卡片状态文字共用。 */
const STATUS_PRESENTATION: Record<TerminalSessionStatus, { label: string; dot: string; text: string }> = {
  starting: { label: "启动中", dot: "bg-warning", text: "text-warning" },
  running: { label: "运行中", dot: "bg-success", text: "text-success" },
  exited: { label: "已退出", dot: "bg-muted-foreground", text: "text-muted-foreground" },
  closed: { label: "已关闭", dot: "bg-muted-foreground", text: "text-muted-foreground" },
  failed: { label: "会话失败", dot: "bg-destructive", text: "text-destructive" },
};

export function terminalStatusPresentation(status: TerminalSessionStatus) {
  return STATUS_PRESENTATION[status];
}

export type TerminalTab = {
  session: TerminalSessionEntry;
  title: string;
};

/**
 * 计算标签栏：活跃会话按 snapshot 顺序在前；当前选中的已结束会话（用户从「已结束」打开、或正在看的
 * 会话刚退出）追加在后，以便继续查看最后画面。只有一个标签时显示「终端」不编号；多个时显示
 * 「终端 N」。Agent 指定的 label 优先作为标签名。
 */
export function terminalTabs(sessions: readonly TerminalSessionEntry[], selectedId: string | null): TerminalTab[] {
  const visible = sessions.filter((session) => isActiveTerminalSession(session) || session.sessionId === selectedId);
  const single = visible.length === 1;
  return visible.map((session, index) => ({
    session,
    title: session.label.trim() || (single ? "终端" : `终端 ${index + 1}`),
  }));
}

export function endedTerminalSessions(sessions: readonly TerminalSessionEntry[]): TerminalSessionEntry[] {
  return sessions.filter((session) => !isActiveTerminalSession(session));
}

/**
 * 计算需要挂载视图（xterm + WS）的会话，保持已挂载项的顺序以免 React 重排窗格。
 *
 * - 活跃会话：面板可见时被选中才首次挂载，此后常驻到它结束或从 snapshot 消失——卸载会丢掉超出后端
 *   预览缓冲的历史，切换标签、收起面板都不卸载。
 * - 已结束会话：只在被选中查看时挂载（从 ⋮「已结束」打开，或正在看的会话刚退出），选中别的会话后
 *   立即释放；它们只剩缓冲内的最后画面，常驻只会白占回滚缓冲与连接。
 *
 * 结果与 `current` 内容相同时返回原数组，便于调用方跳过无效渲染。
 */
export function mountedTerminalSessionIds(
  current: readonly string[],
  sessions: readonly TerminalSessionEntry[],
  selectedId: string | null,
  open: boolean,
): readonly string[] {
  const byId = new Map(sessions.map((session) => [session.sessionId, session]));
  const kept = current.filter((id) => {
    const session = byId.get(id);
    return session !== undefined && (isActiveTerminalSession(session) || id === selectedId);
  });
  const next = open && selectedId !== null && byId.has(selectedId) && !kept.includes(selectedId)
    ? [...kept, selectedId]
    : kept;
  return next.length === current.length && next.every((id, index) => id === current[index]) ? current : next;
}
