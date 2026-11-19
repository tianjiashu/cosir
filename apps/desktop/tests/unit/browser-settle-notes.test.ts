import { describe, expect, it } from "vitest";

import { SettleNotes, type SettleReport } from "@/electron/main/browser/settle-notes";
import { TabLog, type NetworkEntry } from "@/electron/main/browser/tab-log";

/** 记一条在途请求并取回它的条目。 */
function request(log: TabLog, id: string, url: string, timestamp = 1): NetworkEntry {
  const from = log.nextSeq;
  log.requestStarted(id, { method: "GET", url, resourceType: "Fetch", timestamp });
  return log.read({ kind: "network", since: from, failedOnly: false, limit: 1 }).entries[0];
}
const report = (change: Partial<SettleReport> = {}): SettleReport => ({ waitedMs: 2_000, pending: [], changing: null, unwaited: [], ...change });

describe("SettleNotes", () => {
  // 目的：落定结论要在 Agent 拿到结果时成立；潜在缺陷：一次 act 里前几步没回来的请求被后一步的结论替换掉，
  // 或第一步没等到、第二步期间已回来的请求仍报「还在跑」。
  it("lists every request still running when the result is taken, across the actions", () => {
    const log = new TabLog();
    const notes = new SettleNotes();
    const a = request(log, "a", "https://x.test/api/a");
    const b = request(log, "b", "https://x.test/api/b");
    const c = request(log, "c", "https://x.test/api/c");
    notes.record(report({ pending: [a, b] }));
    log.requestFinished("b", 2, 10);
    notes.record(report({ pending: [a, c] }));
    expect(notes.take(log)).toEqual([expect.stringMatching(/^2 requests that may carry page content are still running: GET https:\/\/x\.test\/api\/a, GET https:\/\/x\.test\/api\/c\./)]);
    expect(notes.take(log)).toEqual([]);
  });

  it("says nothing about requests that all came back before the result", () => {
    const log = new TabLog();
    const notes = new SettleNotes();
    notes.record(report({ pending: [request(log, "a", "https://x.test/api/a")] }));
    log.requestFinished("a", 2, 10);
    expect(notes.take(log)).toEqual([]);
  });

  // 目的：轮播页上每个动作都「还在变」，只该说一遍并说清几步；潜在缺陷：十个动作叠出十条几乎相同的备注。
  it("sums up a changing page over the actions in one note", () => {
    const log = new TabLog();
    const notes = new SettleNotes();
    notes.record(report({ changing: { count: 1, example: "tick 1" } }));
    notes.record(report());
    notes.record(report({ waitedMs: 2_400, changing: { count: 2, example: "tick 9" } }));
    expect(notes.take(log)).toEqual([expect.stringMatching(/^the page was still changing when waiting ended after 2 of the 3 actions \(last time: waited 2\.4s; 2 lines differed between the last two reads, e\.g\. tick 9\)/)]);
    notes.record(report({ changing: { count: 1, example: "tick 12" } }));
    expect(notes.take(log)).toEqual([expect.stringMatching(/^the page was still changing when waiting ended \(waited 2\.0s; 1 line differed/)]);
  });

  // 目的：新的操作开始时，上一次没交出去的判断不再成立；潜在缺陷：观察失败后旧结论混进下一次操作的结果。
  it("forgets the previous operation's conclusions when a new one begins", () => {
    const log = new TabLog();
    const notes = new SettleNotes();
    notes.record(report({ changing: { count: 1, example: "old" } }));
    notes.begin();
    expect(notes.take(log)).toEqual([]);
  });

  // 目的：识别规则覆盖不到的情况要让 Agent 知道有请求没等；潜在缺陷：埋点页面每一步都重复同一句提示，
  // 或提示还没交付就记成已提示、或报出已经回来的请求。
  it("hints at each unwaited endpoint once per page, at delivery and only while it is still running", () => {
    const log = new TabLog();
    const notes = new SettleNotes();
    const done = request(log, "b1", "https://t.test/beacon?n=1");
    log.requestFinished("b1", 1.5, 10);
    notes.record(report({ unwaited: [done] }));
    expect(notes.take(log)).toEqual([]);

    notes.record(report({ unwaited: [request(log, "b2", "https://t.test/beacon?n=2", 2), request(log, "b3", "https://t.test/beacon?n=3", 3)] }));
    expect(notes.take(log)).toEqual([expect.stringMatching(/^not waited for: 1 request still in progress to an endpoint .*\(GET https:\/\/t\.test\/beacon\)/)]);
    notes.record(report({ unwaited: [request(log, "b4", "https://t.test/beacon?n=4", 4)] }));
    expect(notes.take(log)).toEqual([]);

    log.navigationStarted();
    log.pageStarted();
    notes.record(report({ unwaited: [request(log, "b5", "https://t.test/beacon?n=5", 5)] }));
    expect(notes.take(log)).toHaveLength(1);
  });
});
