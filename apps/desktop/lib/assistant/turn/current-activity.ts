import type { BackgroundTaskEntry, TransportActivity, TransportToolCallPart } from "../contract";
import { toolHeading } from "../tools/tool-catalog";

export type ActivityFeedback = { label: string; animated: boolean };
export const STOPPING_FEEDBACK: ActivityFeedback = { label: "正在停止…", animated: false };

/** 只读当前回复与后台登记，不扫描历史、不从意图推断执行状态。 */
export function currentActivity(activity: TransportActivity | null, calls: readonly TransportToolCallPart[] = [], background: readonly BackgroundTaskEntry[] = []): ActivityFeedback | null {
  if (!activity || !["running", "suspended"].includes(activity.status)) return null;
  if (calls.some(call => call.awaitingUser)) return { label: "等待你的操作", animated: false };
  const jobs = background.filter(job => job.status === "running" || job.status === "settling");
  const suffix = jobs.length ? ` · 后台 ${jobs.length} 项` : "";
  const model = activity.modelActivity;
  // 输出正文时正文本身就是反馈；其余模型阶段对用户是同一件事，内部阶段上屏只会在工具调用之间来回闪。
  if (model?.phase === "responding") return null;
  if (model) return { label: `正在思考${suffix}`, animated: true };
  const foreground = calls.filter(call => !call.background && !call.awaitingUser && call.presentation !== "quiet" && (call.status === "running" || call.status === "pending"));
  if (foreground.length) {
    const first = foreground[0];
    return { label: toolHeading(first.toolName, first.args, first.intent).title + (foreground.length > 1 ? ` · 另 ${foreground.length - 1} 项` : "") + suffix, animated: true };
  }
  if (jobs.length) {
    const allSettling = jobs.every(job => job.status === "settling");
    const label = jobs.length === 1 ? jobs[0].label : `${jobs.length} 项会话`;
    return { label: `${allSettling ? "等待并入后台结果" : "后台执行"}：${label}`, animated: !allSettling && activity.status === "running" };
  }
  return activity.status === "suspended" ? { label: "等待继续", animated: false } : { label: "正在处理", animated: true };
}
