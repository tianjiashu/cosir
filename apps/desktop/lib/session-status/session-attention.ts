import type { SessionStatus } from "./session-status-store";

/**
 * 侧栏一类列表上，一个会话此刻要不要引起你的注意、为什么。按「你要做什么」排优先级，同一时刻只取一个：
 *
 * 1. `approval` 待审批：终端命令停着等你批准，最急——不批它就不往下走；
 * 2. `answer` 待回答：Agent 问了你问题；
 * 3. `working` 进行中：Run 在跑，或挂起等后台结果（还没结局，你不用做什么）；
 * 4. `failed` 失败、你还没看；
 * 5. `unread` 完成、你还没看。
 *
 * 其余（已看过、被停下、没有 Run）是 null，列表上不留标记。在等你的两类与 Run 状态无关：Run 已停下时审批仍可能在等。
 * `viewing` 是你正开着这个会话：未读马上会被已读命令清掉（`use-mark-session-read.ts`），这里先不显示，免得闪一下。
 * 「看过」的定义就是「停在这个会话上」，有意不等正文渲染（理由与改动约束见 `use-mark-session-read.ts`）。
 */
export type SessionAttention = "approval" | "answer" | "working" | "failed" | "unread";

const WORKING_STATUSES = new Set(["running", "suspended"]);

export function sessionAttention(status: SessionStatus | null | undefined, viewing = false): SessionAttention | null {
  if (!status) return null;
  if (status.awaitingApprovals > 0) return "approval";
  if (status.awaitingDecisions > 0) return "answer";
  if (status.executionStatus !== null && WORKING_STATUSES.has(status.executionStatus)) return "working";
  if (!status.unread || viewing) return null;
  return status.executionStatus === "failed" ? "failed" : "unread";
}
