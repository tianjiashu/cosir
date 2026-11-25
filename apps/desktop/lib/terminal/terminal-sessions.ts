import type { TerminalSessionEntry, TerminalSessionStatus } from "@/lib/assistant/contract";

/**
 * 终端会话的展示规则（纯函数）：生命周期分组、状态文案与标签命名。
 *
 * 会话事实只来自 snapshot 的 `terminal_sessions`，这里不做任何推断，只决定「怎么显示」；
 * 标签栏、菜单与页头角标共用同一套规则。
 */

export function isActiveTerminalSession(session: Pick<TerminalSessionEntry, "status">): boolean {
  return session.status === "starting" || session.status === "running";
}

/** `dot` / `text` 是 Tailwind 语义色类（`dot` 用于标签状态点）。 */
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
 * 「终端 N」。
 *
 * N 取会话在完整列表（含已结束）里的序号，而不是在可见标签里的位置：关掉「终端 1」后「终端 2」
 * 不会改名为「终端 1」，关闭确认框里引用的标题也就始终指向用户看到的那个标签。
 */
export function terminalTabs(sessions: readonly TerminalSessionEntry[], selectedId: string | null): TerminalTab[] {
  const ordinals = new Map(sessions.map((session, index) => [session.terminalSessionId, index + 1]));
  const visible = sessions.filter((session) => isActiveTerminalSession(session) || session.terminalSessionId === selectedId);
  const single = visible.length === 1;
  return visible.map((session) => ({
    session,
    title: single ? "终端" : `终端 ${ordinals.get(session.terminalSessionId)}`,
  }));
}

/**
 * 用户看到的终端名：在标签栏里就用标签名（只有一个时叫「终端」），已结束的按它在标签栏时的编号（「终端 N」），
 * 与用户见过的名字一致；入口提醒与工具调用行共用。不在列表里（如 Agent 自己运行中的命令）为 null。
 */
export function terminalTitle(sessions: readonly TerminalSessionEntry[], terminalSessionId: string): string | null {
  const index = sessions.findIndex((session) => session.terminalSessionId === terminalSessionId);
  if (index === -1) return null;
  return terminalTabs(sessions, null).find((tab) => tab.session.terminalSessionId === terminalSessionId)?.title ?? `终端 ${index + 1}`;
}

/** 异常结束：会话失败，或 shell 以非 0 退出码退出；用户主动关闭不算。 */
function isAbnormalTerminalEnd(session: Pick<TerminalSessionEntry, "status" | "exitCode">): boolean {
  return session.status === "failed" || (session.status === "exited" && session.exitCode !== null && session.exitCode !== 0);
}

/**
 * 镜像从 `previous` 换成 `next` 时，新变成「Agent 填入、等你回车」的终端（多个取最后一个）；工作台据此展开面板。
 *
 * 只认这一刻的翻转：`previous` 为 null（页面刚拿到首帧、刷新后重连）时不算，免得每次进页面都弹一次；
 * 新出现就带着待回车的终端（Agent 刚为填入新开的）算翻转。
 */
export function newlyPrefilled(
  previous: readonly TerminalSessionEntry[] | null,
  next: readonly TerminalSessionEntry[],
): string | null {
  if (previous === null) return null;
  const pending = new Set(previous.filter((session) => session.prefillPending).map((session) => session.terminalSessionId));
  return next.findLast((session) => session.prefillPending && !pending.has(session.terminalSessionId))?.terminalSessionId ?? null;
}

/**
 * 页头终端入口的状态：会话数写进入口的 tooltip 与无障碍标签（不画数字角标），有提醒时画点，
 * 让面板收起时也能看出终端需要处理。
 *
 * - `attention`（红点）：有用户还没看过的异常结束（打开面板即视为看过，见 store 的 `acknowledgedEnds`）；
 * - `activity`（蓝点）：有 Agent 填入的命令在等用户回车，用户接手或执行后自然消失；
 * - `null`：无需提醒。异常结束优先：它是出了问题，待回车只是在等你。
 */
export type TerminalBadge = {
  count: number;
  tone: "attention" | "activity" | null;
  /** 提醒的一句话说明（拼进入口的 tooltip 与无障碍标签）；无提醒为 null。 */
  hint: string | null;
};

export function terminalBadge(
  sessions: readonly TerminalSessionEntry[] | null,
  acknowledgedEnds: readonly string[],
): TerminalBadge {
  if (sessions === null) return { count: 0, tone: null, hint: null };
  const count = terminalTabs(sessions, null).length;
  const failed = sessions.filter((session) => isAbnormalTerminalEnd(session) && !acknowledgedEnds.includes(session.terminalSessionId));
  if (failed.length > 0) {
    const [first] = failed;
    const detail = first.status === "failed" ? "会话失败" : `退出码 ${first.exitCode}`;
    const more = failed.length > 1 ? `等 ${failed.length} 个终端` : "";
    return { count, tone: "attention", hint: `「${terminalTitle(sessions, first.terminalSessionId)}」${more}异常结束（${detail}）` };
  }
  const waiting = sessions.find((session) => session.prefillPending);
  if (waiting) return { count, tone: "activity", hint: `「${terminalTitle(sessions, waiting.terminalSessionId)}」里有 Agent 填入的命令等你回车` };
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
 * - 已结束会话：只在被选中查看时挂载（从页头「已结束的终端」菜单打开，或正在看的会话刚退出），选中别的会话后
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
  const byId = new Map(sessions.map((session) => [session.terminalSessionId, session]));
  const kept = current.filter((id) => {
    const session = byId.get(id);
    return session !== undefined && (isActiveTerminalSession(session) || id === selectedId);
  });
  const next = open && selectedId !== null && byId.has(selectedId) && !kept.includes(selectedId)
    ? [...kept, selectedId]
    : kept;
  return next.length === current.length && next.every((id, index) => id === current[index]) ? current : next;
}
