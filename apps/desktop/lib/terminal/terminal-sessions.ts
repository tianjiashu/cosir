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
 *
 * N 取会话在完整列表（含已结束）里的序号，而不是在可见标签里的位置：关掉「终端 1」后「终端 2」
 * 不会改名为「终端 1」，关闭确认框里引用的标题也就始终指向用户看到的那个标签。
 */
export function terminalTabs(sessions: readonly TerminalSessionEntry[], selectedId: string | null): TerminalTab[] {
  const ordinals = new Map(sessions.map((session, index) => [session.sessionId, index + 1]));
  const visible = sessions.filter((session) => isActiveTerminalSession(session) || session.sessionId === selectedId);
  const single = visible.length === 1;
  return visible.map((session) => ({
    session,
    title: session.label.trim() || (single ? "终端" : `终端 ${ordinals.get(session.sessionId)}`),
  }));
}

/** 异常结束：会话失败，或 shell 以非 0 退出码退出；用户 / Agent 主动关闭不算。 */
export function isAbnormalTerminalEnd(session: Pick<TerminalSessionEntry, "status" | "exitCode">): boolean {
  return session.status === "failed" || (session.status === "exited" && session.exitCode !== null && session.exitCode !== 0);
}

/**
 * 页头终端入口的角标：数字是活跃会话数，色调与提示让面板收起时也能看出终端在发生什么。
 *
 * - `attention`：有用户还没看过的异常结束（打开面板即视为看过，见 store 的 `acknowledgedEnds`）；
 * - `null`：无需提醒。
 */
export type TerminalBadge = {
  count: number;
  tone: "attention" | null;
  /** 提醒的一句话说明（拼进入口的 tooltip 与无障碍标签）；无提醒为 null。 */
  hint: string | null;
};

export function terminalBadge(
  sessions: readonly TerminalSessionEntry[] | null,
  acknowledgedEnds: readonly string[],
): TerminalBadge {
  if (sessions === null) return { count: 0, tone: null, hint: null };
  const tabs = terminalTabs(sessions, null);
  // 已结束的会话不在标签栏里，按它在标签栏时的编号称呼（「终端 N」），与用户看到过的名字一致。
  const titleOf = (session: TerminalSessionEntry) => (
    tabs.find((tab) => tab.session.sessionId === session.sessionId)?.title
      ?? (session.label.trim() || `终端 ${sessions.indexOf(session) + 1}`)
  );
  const count = tabs.length;
  const failed = sessions.filter((session) => isAbnormalTerminalEnd(session) && !acknowledgedEnds.includes(session.sessionId));
  if (failed.length > 0) {
    const [first] = failed;
    const detail = first.status === "failed" ? "会话失败" : `退出码 ${first.exitCode}`;
    const more = failed.length > 1 ? `等 ${failed.length} 个终端` : "";
    return { count, tone: "attention", hint: `「${titleOf(first)}」${more}异常结束（${detail}）` };
  }
  return { count, tone: null, hint: null };
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
