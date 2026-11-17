import type { BackgroundTaskEntry, TransportActivity, TransportToolCallPart } from "../contract";
import { toolHeading } from "../tools/tool-catalog";

export type ActivityFeedback = { label: string; animated: boolean };
export const STOPPING_FEEDBACK: ActivityFeedback = { label: "正在停止…", animated: false };

/**
 * 「Agent 此刻在做什么」的唯一推导。用户感知的单位是活动段：两次中断（旁白、独立工具、Agent 来信）之间的
 * 思考与工具调用是同一段工作，只在那一段的那一行说（见 `turn-layout` 的 `ActivityGroup.live`）。独立工具与
 * 等你操作的卡片由它们自己那一行说；正在输出旁白时旁白本身就是反馈。
 */

/** 调用是否正由前台执行（含模型正在写它的参数）：后台归属以看板为准，等你操作的由卡片自己表达。 */
export function executingInForeground(call: TransportToolCallPart, board: ReadonlySet<string>): boolean {
  return (call.status === "running" || call.status === "pending") && !board.has(call.toolCallId) && !call.background && !call.awaitingUser;
}

/**
 * 一个活动段此刻的状态；没有要说的时为 null（段显示汇总，没有调用的空段不出现）。
 *
 * 段内有前台执行的调用时说它：真正执行中的（`running`）优先，排队的 `pending` 不冒充——批次先串行后并行，
 * 排在前面的调用未必在跑；只有排队的调用时（模型在写参数或刚写完）说第一个的标题。后台归属以看板为准：看板
 * 由 part 上的后台记录加登记表活条目投影，活条目在占位写入前就可见，只看 part 会让转后台的那一瞬同一调用仍算
 * 前台；看板只随会话最后一条回复下发，拿不到看板时才只剩 part 自己的记录。
 *
 * 否则只有回合的最后一段说无归属的状态（`unowned`，见 {@link unownedActivity}）。
 */
export function segmentActivity(calls: readonly TransportToolCallPart[], board: ReadonlySet<string>, unowned: ActivityFeedback | null): ActivityFeedback | null {
  const executing = calls.filter(call => executingInForeground(call, board));
  if (!executing.length) return unowned;
  const lead = executing.find(call => call.status === "running") ?? executing[0];
  const more = executing.length > 1 ? ` · 另 ${executing.length - 1} 项` : "";
  return { label: toolHeading(lead.toolName, lead.args, lead.intent).title + more, animated: true };
}

/**
 * 不属于任何调用的状态：模型在思考、Session 在等待。只读当前活动与后台登记，不扫描历史、不从意图推断执行状态。
 *
 * 运行中：输出正文时为 null（正文本身就是反馈）——但要等正文真的在回合末尾流出来（`proseStreaming`）：模型
 * 进入输出阶段后，首段正文还会在后端缓冲片刻才到，这期间仍说思考，否则这一行先消失、正文后出现，回合底部跳
 * 一下。模型写工具参数时说「正在准备工具」（调用行到齐参数前只有动词）；其余都是思考——工具结束到下一次模型请求之间也是，不另起「正在处理」，否则两种文案在每一步之间来回闪。
 * 后台作业在运行中只作后缀：Agent 在推进时不是在等它们；挂起时才由后台作业占据主文案。
 */
export function unownedActivity(activity: TransportActivity | null, background: readonly BackgroundTaskEntry[] = [], proseStreaming = true): ActivityFeedback | null {
  if (!activity || !["running", "suspended"].includes(activity.status)) return null;
  const jobs = background.filter(job => job.status === "running" || job.status === "settling");
  if (activity.status === "suspended") {
    if (!jobs.length) return { label: "等待继续", animated: false };
    const allSettling = jobs.every(job => job.status === "settling");
    const label = jobs.length === 1 ? jobs[0].label : `${jobs.length} 项会话`;
    return { label: `${allSettling ? "等待并入后台结果" : "后台执行"}：${label}`, animated: false };
  }
  const model = activity.modelActivity;
  if (model?.phase === "responding" && proseStreaming) return null;
  const suffix = jobs.length ? ` · 后台 ${jobs.length} 项` : "";
  return { label: (model?.phase === "tool_call" ? "正在准备工具" : "思考中") + suffix, animated: true };
}
