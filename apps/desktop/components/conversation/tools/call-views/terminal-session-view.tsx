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
  command_ended: "命令已结束",
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
 * 元信息按载荷字段判别动作，不按工具名：`terminal_count` 是列表；`input_summary` 是填入；`lines` /
 * `screen` 是读取。
 */
function TerminalMeta({ data }: { data: Record<string, unknown> }) {
  if (typeof data.terminal_count === "number") return <>{data.terminal_count} 个终端</>;
  const wait = waitResultLabel(data.wait_result);
  const summary = text(data.input_summary);
  if (summary !== undefined) return <>{[summary, wait].filter(Boolean).join(" · ")}</>;
  if (data.screen === true || Array.isArray(data.lines)) {
    return <>{[data.screen === true ? "全屏画面" : lineRange(data.lines), wait].filter(Boolean).join(" · ") || "读取输出"}</>;
  }
  return null;
}

/**
 * 打开终端面板定位到用户会话：只读线程（子 Agent）与已失效会话不提供跳转。运行中的命令不在会话列表里，
 * 因此自然不显示这个按钮；会话是否还在读 snapshot 的实时会话列表（镜像尚未同步与会话已消失都按已失效
 * 处理：行挂载时镜像已由同一份 snapshot 同步好）。
 */
function RevealTerminalAction({ data, taskId }: { data: Record<string, unknown>; taskId: number; }) {
  const terminalId = text(data.terminal_id);
  const live = useTerminalWorkbenchStore((state) => (
    terminalId !== undefined && (state.tasks[taskId]?.sessions?.some((session) => session.sessionId === terminalId) ?? false)
  ));
  if (terminalId === undefined || !live) return null;
  return <CallOpenButton label="在终端中打开" onClick={() => useTerminalWorkbenchStore.getState().reveal(taskId, terminalId)} />;
}

/**
 * `terminal-session`（`terminal_*`）：标题与目标由前端工具目录按工具名 + 参数算出，这里只投影结果——
 * 没有展开体（终端输出在终端面板里看）。
 */
export const terminalSessionView: CallView = (data, context) => {
  return {
    meta: <TerminalMeta data={data} />,
    actions: (context.interactive ? <RevealTerminalAction data={data} taskId={context.taskId} /> : null),
  };
};
