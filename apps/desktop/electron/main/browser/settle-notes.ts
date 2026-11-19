import type { NetworkEntry, TabLog } from "./tab-log";

/** 备注里最多列出几条请求。 */
const REQUESTS_LISTED = 3;

/** 一次落定记下的结论（`BrowserTab.settle`）：只记事实，文案到交付时才对照当时的状态写。 */
export type SettleReport = {
  waitedMs: number;
  /** 等到上限时还没回来、可能带来内容的请求。 */
  pending: NetworkEntry[];
  /** 等到上限时页面还在变：最后两次读取有几行不同、举一行为例。 */
  changing: { count: number; example: string | null } | null;
  /** 按后台流量没等、落定结束时仍在途的请求。 */
  unwaited: NetworkEntry[];
};

/**
 * 一次修改操作（导航或一次 act 的全部动作）的落定结论，交付页面结果时写成给 Agent 的备注。
 *
 * 落定结论与标签日志里的页面事件是两种事实：事件是发生过的事，发生时就成立，逐条交付；落定结论是对页面状态的判断，
 * 要在 Agent 拿到结果的那一刻成立。所以每一步落定只记结构（`record`），文案在页面结果处对照此刻的状态生成
 * （`take`）：已经回来的请求不再报「还没回来」，一次 act 的多个动作汇总成一条、按动作数说清。
 *
 * 生命周期：新的修改操作开始时清空（`begin`，没交出去的旧判断不再成立），页面结果取走即清空。结论不进标签日志，
 * 标签自己关掉时也不转交给接手的标签——它说的是一个已经不存在的页面。观察失败时结论留到下一次页面结果（那是这次
 * 操作之后的第一次观察），照样对照那时的状态写。
 */
export class SettleNotes {
  private reports: SettleReport[] = [];

  begin(): void {
    this.reports = [];
  }

  record(report: SettleReport): void {
    this.reports.push(report);
  }

  /** 写成备注并清空。「没等」的提示同一接口一份文档只说一次（`TabLog.noticeUnwaited`），在这里交付时才记为已提示。 */
  take(log: TabLog): string[] {
    const reports = this.reports;
    this.reports = [];
    if (reports.length === 0) return [];
    const next = "if something you expect is missing, use a wait_for action for text you expect, or read a fresh browser_snapshot shortly.";
    const notes: string[] = [];

    const pending = unique(reports.flatMap((report) => report.pending)).filter((entry) => log.running(entry));
    if (pending.length > 0) {
      const one = pending.length === 1;
      notes.push(`${pending.length} request${one ? "" : "s"} that may carry page content ${one ? "is" : "are"} still running: ${listRequests(log, pending)}. Parts of the page may not have rendered yet: ${next}`);
    }

    const changing = reports.filter((report) => report.changing !== null);
    if (changing.length > 0) {
      const last = changing.at(-1)!;
      const { count, example } = last.changing!;
      const steps = reports.length === 1 ? "" : changing.length === reports.length ? ` after all ${reports.length} actions` : ` after ${changing.length} of the ${reports.length} actions`;
      const evidence = `waited ${(last.waitedMs / 1000).toFixed(1)}s; ${count} line${count === 1 ? "" : "s"} differed between the last two reads${example ? `, e.g. ${example}` : ""}`;
      notes.push(`the page was still changing when waiting ended${steps} (${changing.length > 1 ? "last time: " : ""}${evidence}). It may be live content such as a timer, carousel or streaming text: ${next}`);
    }

    // 识别规则覆盖不到的情况（同一操作的串行请求、看起来像重复的内容请求）交给 Agent 判断。
    const unwaited = new Map<string, NetworkEntry>();
    for (const entry of unique(reports.flatMap((report) => report.unwaited))) {
      if (!log.running(entry)) continue;
      const endpoint = log.endpoint(entry);
      if (!unwaited.has(endpoint) && log.noticeUnwaited(endpoint)) unwaited.set(endpoint, entry);
    }
    if (unwaited.size > 0) {
      const one = unwaited.size === 1;
      notes.push(`not waited for: ${unwaited.size} request${one ? "" : "s"} still in progress to ${one ? "an endpoint" : "endpoints"} this page calls repeatedly, most likely background traffic such as analytics or polling (${listRequests(log, [...unwaited.values()])}). If content you expect is missing, use a wait_for action for text you expect, or browser_logs with kind network to see whether ${one ? "it" : "they"} finished.`);
    }
    return notes;
  }
}

function unique(entries: NetworkEntry[]): NetworkEntry[] {
  return [...new Set(entries)];
}

/** 列出请求：方法加接口身份（不带查询参数，它常带令牌），最多 `REQUESTS_LISTED` 条。 */
function listRequests(log: TabLog, entries: NetworkEntry[]): string {
  const listed = entries.slice(0, REQUESTS_LISTED).map((entry) => `${entry.method} ${log.endpoint(entry)}`).join(", ");
  return entries.length > REQUESTS_LISTED ? `${listed} and ${entries.length - REQUESTS_LISTED} more` : listed;
}
