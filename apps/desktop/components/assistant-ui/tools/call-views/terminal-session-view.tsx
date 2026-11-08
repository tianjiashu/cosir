import type { TerminalSessionEntry } from "@/lib/assistant/contract";
import { terminalStatusPresentation } from "@/lib/terminal/terminal-sessions";
import { useTerminalWorkbenchStore } from "@/lib/terminal/terminal-workbench-store";
import { CallOpenButton } from "../call-open-button";
import type { CallView } from "../call-view";
import { asRecord, readText as text } from "../display-data-readers";

/**
 * 条件等待的结束原因（后端 `wait_result.end_reason`）→ 中文；不据此宣告命令成功。`idle` 为未带等待条件。
 */
const WAIT_END_REASON_LABELS: Record<string, string> = {
  command_returned: "命令已返回",
  matched: "匹配到输出",
  output_quiet: "输出已静默",
  elapsed: "等待结束",
  timeout: "等待超时",
  cancelled: "等待已取消",
  session_ended: "会话已结束",
};

function waitResultLabel(value: unknown): string | null {
  const reason = text(asRecord(value).end_reason);
  if (!reason || reason === "idle") return null;
  return WAIT_END_REASON_LABELS[reason] ?? reason;
}

/** `lines` 为本次返回的行号区间 `[起始行, 结束行]`；无新增行时为 null。 */
function lineRange(value: unknown): string | null {
  if (!Array.isArray(value) || value.length !== 2) return null;
  const [start, end] = value;
  if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
  return start === end ? `第 ${start} 行` : `第 ${start}–${end} 行`;
}

/**
 * 行上的会话状态读 snapshot 的实时会话列表（Task 级），而不是工具执行那一刻的快照。
 * 「镜像尚未同步（sessions 为 null）」与「会话已消失」都返回 null、按已失效处理：行挂载时镜像已由同一份
 * snapshot 同步好。
 */
function useLiveSession(taskId: number, sessionId: string | undefined): TerminalSessionEntry | null {
  return useTerminalWorkbenchStore((state) => (
    sessionId === undefined
      ? null
      : state.tasks[taskId]?.sessions?.find((session) => session.sessionId === sessionId) ?? null
  ));
}

/**
 * 元信息按载荷字段判别动作，不按工具名：`session_count` 是列表；`input_summary` 是输入 / 交接；`lines` /
 * `screen` 是读取；其余（start / close）显示会话的实时状态与退出码。
 */
function TerminalSessionMeta({ data, taskId }: { data: Record<string, unknown>; taskId: number; }) {
  const sessionId = text(data.session_id);
  const live = useLiveSession(taskId, sessionId);
  return <SessionMeta data={data} live={live} />;
}

/** 历史只读调用时的快照，实时会话由上面的拥有方组件订阅。 */
function SessionMeta({ data, live, inherited = false }: { data: Record<string, unknown>; live: TerminalSessionEntry | null; inherited?: boolean }) {
  const sessionId = text(data.session_id);
  if (typeof data.session_count === "number") return <>{data.session_count} 个会话</>;
  const wait = waitResultLabel(data.wait_result);
  const summary = text(data.input_summary);
  if (summary !== undefined) return <>{[summary, wait].filter(Boolean).join(" · ")}</>;
  if (data.screen === true || Array.isArray(data.lines)) {
    return <>{[data.screen === true ? "全屏画面" : lineRange(data.lines), wait].filter(Boolean).join(" · ") || "读取输出"}</>;
  }
  const status = live ? terminalStatusPresentation(live.status)
    : data.status === "closed" || data.status === "exited" || data.status === "failed" ? terminalStatusPresentation(data.status) : null;
  const items = [
    text(data.shell_kind),
    status?.label ?? (inherited ? "继承的会话记录" : sessionId ? "已失效" : null),
    typeof data.exit_code === "number" ? `exit ${data.exit_code}` : null,
  ].filter(Boolean);
  return items.length > 0 ? <>{items.join(" · ")}</> : null;
}

/** 打开终端面板定位到会话：只读线程（子 Agent）与已失效会话不提供跳转。 */
function RevealSessionAction({ data, taskId }: { data: Record<string, unknown>; taskId: number; }) {
  const sessionId = text(data.session_id);
  const live = useLiveSession(taskId, sessionId);
  if (sessionId === undefined || live === null) return null;
  return <CallOpenButton label="在终端中打开" onClick={() => useTerminalWorkbenchStore.getState().reveal(taskId, sessionId)} />;
}

/**
 * `terminal-session`（`terminal_*`）：标题由后端 `heading` 给出（会话名、输入摘要），这里只投影结果——
 * 没有展开体（终端输出在终端面板里看）。交接（`terminal_handoff`）等待用户时由等待卡承载交还入口，行上只有
 * 通用的「等你操作」。
 */
export const terminalSessionView: CallView = (data, context) => {
  return {
    meta: context.inherited ? <SessionMeta data={data} live={null} inherited /> : <TerminalSessionMeta data={data} taskId={context.taskId} />,
    actions: (context.interactive ? <RevealSessionAction data={data} taskId={context.taskId} /> : null),
  };
};
