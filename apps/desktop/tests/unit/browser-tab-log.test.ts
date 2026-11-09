import { describe, expect, it } from "vitest";

import { CANCELED_FAILURE, TabLog } from "@/electron/main/browser/tab-log";

const consoleEntry = (level: "debug" | "info" | "warning" | "error", text: string) => ({ level, text, source: null, frame: null });

describe("TabLog", () => {
  // 目的：「上次以来」只在页面结果处消费；潜在缺陷：只为拿标签 id 的调用（交接）吞掉了下载完成之类的备注。
  it("hands notes and new-log counts to page results only once, and reading logs consumes nothing", () => {
    const log = new TabLog();
    log.note("a download started");
    log.console(consoleEntry("error", "boom"));
    log.console(consoleEntry("warning", "careful"));
    log.requestStarted("r1", { method: "GET", url: "http://a/x", resourceType: "Fetch", timestamp: 1 });
    log.responseReceived("r1", 500, 1.02);
    log.requestFinished("r1", 1.03);
    expect(log.undeliveredNotes()).toEqual(["a download started"]);
    expect(log.read({ kind: "console", limit: 50 }).entries).toHaveLength(2);

    expect(log.digest()).toEqual({ notes: ["a download started"], newLogs: { consoleErrors: 1, consoleWarnings: 1, failedRequests: 1 } });
    expect(log.digest()).toEqual({ notes: [], newLogs: { consoleErrors: 0, consoleWarnings: 0, failedRequests: 0 } });
    expect(log.undeliveredNotes()).toEqual([]);
    expect(log.read({ kind: "network", failedOnly: true, limit: 50 }).entries).toEqual([
      { seq: 4, method: "GET", url: "http://a/x", status: 500, resourceType: "Fetch", failure: null, durationMs: 30 },
    ]);
  });

  // 目的：失败数按落定时刻计；潜在缺陷：页面结果组装时还在途、之后才失败的请求（SPA 首屏接口）永远不计入。
  it("counts a request that fails after a page result in the next one, once", () => {
    const log = new TabLog();
    log.requestStarted("api", { method: "GET", url: "http://a/api/me", resourceType: "Fetch", timestamp: 1 });
    expect(log.digest().newLogs.failedRequests).toBe(0);
    log.responseReceived("api", 500, 1.4);
    log.requestFailed("api", "net::ERR_ABORTED", 1.5);
    expect(log.digest().newLogs.failedRequests).toBe(1);
    expect(log.digest().newLogs.failedRequests).toBe(0);
  });

  // 目的：计数与不带 since 的默认读取同一范围；潜在缺陷：离开页面时被取消的在途请求、上一页的报错被计为
  // 「新的」，模型照提示去读却什么也读不到。
  it("counts only the current page and leaves cancelled requests out", () => {
    const log = new TabLog();
    log.console(consoleEntry("error", "old page"));
    log.requestStarted("poll", { method: "GET", url: "http://a/poll", resourceType: "Fetch", timestamp: 1 });
    log.navigationStarted();
    log.requestFailed("poll", CANCELED_FAILURE, 1.1);
    log.pageStarted();
    log.requestStarted("abort", { method: "GET", url: "http://a/aborted", resourceType: "Fetch", timestamp: 1.2 });
    log.requestFailed("abort", CANCELED_FAILURE, 1.3);
    log.requestStarted("api", { method: "GET", url: "http://a/api", resourceType: "Fetch", timestamp: 1.2 });
    log.responseReceived("api", 503, 1.4, true);
    expect(log.digest().newLogs).toEqual({ consoleErrors: 0, consoleWarnings: 0, failedRequests: 1 });
    expect(log.read({ kind: "network", failedOnly: true, limit: 50 }).entries.map((entry) => entry.url)).toEqual(["http://a/aborted", "http://a/api"]);
  });

  // 目的：读新条目不漏不重；潜在缺陷：游标按条数而不是序号，翻页时跳过或重复，或没告诉模型「后面还有」。
  it("pages forward from a cursor and tails the current page without one", () => {
    const log = new TabLog();
    for (const index of [1, 2, 3, 4, 5]) log.console(consoleEntry(index % 2 ? "info" : "error", `m${index}`));
    const tail = log.read({ kind: "console", limit: 2 });
    expect(tail.entries.map((entry) => entry.text)).toEqual(["m4", "m5"]);
    expect(tail).toMatchObject({ next: 6, moreBefore: true, moreAfter: false, droppedBefore: null });

    const first = log.read({ kind: "console", since: 1, limit: 2 });
    expect(first.entries.map((entry) => entry.text)).toEqual(["m1", "m2"]);
    expect(first).toMatchObject({ next: 3, moreBefore: false, moreAfter: true, droppedBefore: null });
    const second = log.read({ kind: "console", since: first.next, limit: 2 });
    expect(second.entries.map((entry) => entry.text)).toEqual(["m3", "m4"]);
    const last = log.read({ kind: "console", since: second.next, limit: 2 });
    expect(last).toMatchObject({ next: 6, moreAfter: false });
    expect(log.read({ kind: "console", since: 6, limit: 2 }).entries).toEqual([]);

    expect(log.read({ kind: "console", level: "error", limit: 50 }).entries.map((entry) => entry.text)).toEqual(["m2", "m4"]);
    expect(log.read({ kind: "console", pattern: "M3", limit: 50 }).entries.map((entry) => entry.text)).toEqual(["m3"]);
  });

  // 目的：「当前页面」从导航开始算；潜在缺陷：主文档请求（含 404 与重定向）在提交前就记下，被当成上一页。
  it("counts the main document's request towards the page it opens", () => {
    const log = new TabLog();
    log.console(consoleEntry("info", "old page"));
    log.navigationStarted();
    log.requestStarted("doc", { method: "GET", url: "http://a/missing", resourceType: "Document", timestamp: 1 });
    log.responseReceived("doc", 404, 1.01);
    log.requestFinished("doc", 1.02);
    log.pageStarted();
    log.console(consoleEntry("error", "new page"));
    expect(log.read({ kind: "network", failedOnly: true, limit: 50 }).entries.map((entry) => entry.url)).toEqual(["http://a/missing"]);
    expect(log.read({ kind: "console", limit: 50 }).entries.map((entry) => entry.text)).toEqual(["new page"]);
    // 没提交的导航（下载、204）不改当前页面：下一次提交才用它的起点。
    log.navigationStarted();
    log.requestStarted("dl", { method: "GET", url: "http://a/file.zip", resourceType: "Document", timestamp: 2 });
    expect(log.read({ kind: "console", limit: 50 }).entries.map((entry) => entry.text)).toEqual(["new page"]);
  });

  // 目的：「导航进行中」只在日志里记一份，标签据它解释加载事件；潜在缺陷：两份各自改写，落定时只清了一份——标签把
  // 已提交页面的加载中断当成换了错误页，或下一页从旧起点算，把上一页的报错计进新页面。
  it("tracks a navigation in flight until it commits or is abandoned", () => {
    const log = new TabLog();
    expect(log.navigating).toBe(false);
    log.navigationStarted();
    expect(log.navigating).toBe(true);
    log.pageStarted();
    expect(log.navigating).toBe(false);

    log.console(consoleEntry("error", "page one"));
    log.navigationStarted();
    log.navigationAbandoned();
    expect(log.navigating).toBe(false);
    log.navigationAbandoned();
    log.console(consoleEntry("error", "still page one"));
    log.pageStarted();
    log.console(consoleEntry("error", "page two"));
    expect(log.read({ kind: "console", limit: 50 }).entries.map((entry) => entry.text)).toEqual(["page two"]);
  });

  // 目的：重定向与失败都要看得到；潜在缺陷：同一 requestId 的第二跳覆盖了第一跳，302 和最终结果只剩一个。
  it("records each redirect hop and failures with durations", () => {
    const log = new TabLog();
    log.requestStarted("r1", { method: "GET", url: "http://a/old", resourceType: "Document", timestamp: 10 });
    log.responseReceived("r1", 302, 10.1, true);
    log.requestStarted("r1", { method: "GET", url: "http://a/new", resourceType: "Document", timestamp: 10.1 });
    log.responseReceived("r1", 200, 10.2);
    log.requestFinished("r1", 10.25);
    log.requestStarted("r2", { method: "POST", url: "http://a/api", resourceType: "XHR", timestamp: 11 });
    log.requestFailed("r2", "net::ERR_CONNECTION_REFUSED", 11.5);
    expect(log.read({ kind: "network", failedOnly: false, limit: 50 }).entries).toEqual([
      { seq: 1, method: "GET", url: "http://a/old", status: 302, resourceType: "Document", failure: null, durationMs: 100 },
      { seq: 2, method: "GET", url: "http://a/new", status: 200, resourceType: "Document", failure: null, durationMs: 150 },
      { seq: 3, method: "POST", url: "http://a/api", status: null, resourceType: "XHR", failure: "net::ERR_CONNECTION_REFUSED", durationMs: 500 },
    ]);
  });

  // 目的：网络请求刷屏不能挤掉备注；被挤掉的条目要如实说明；潜在缺陷：一个轮询接口把「下载完成」挤出缓冲。
  it("keeps capacities per kind and says what was dropped", () => {
    const log = new TabLog();
    for (let index = 0; index < 60; index += 1) log.note(`note ${index}`);
    for (let index = 0; index < 1200; index += 1) {
      log.requestStarted(`r${index}`, { method: "GET", url: `http://a/${index}`, resourceType: "Fetch", timestamp: index });
    }
    const digest = log.digest();
    expect(digest.notes[0]).toBe("(10 earlier notes were dropped)");
    expect(digest.notes).toHaveLength(51);
    expect(digest.notes.at(-1)).toBe("note 59");
    const dropped = log.read({ kind: "network", failedOnly: false, since: 0, limit: 1 });
    expect(dropped).toMatchObject({ droppedBefore: 261, moreAfter: true, next: 262 });
    expect(dropped.entries[0].seq).toBe(261);
    // 从还在的第一条读起就不再报挤掉；console 没被挤过，从 0 读也不报。
    expect(log.read({ kind: "network", failedOnly: false, since: 261, limit: 1 }).droppedBefore).toBeNull();
    expect(log.read({ kind: "console", since: 0, limit: 1 }).droppedBefore).toBeNull();
  });

  // 目的：本类第一条出现在 since 之后不是「挤掉」；潜在缺陷：按缓冲首条判断，读空页后的新条目被误报为丢失。
  it("does not report drops when the first entry of a kind simply came later", () => {
    const log = new TabLog();
    const empty = log.read({ kind: "console", limit: 50 });
    log.requestStarted("r1", { method: "GET", url: "http://a/x", resourceType: "Fetch", timestamp: 1 });
    log.console(consoleEntry("info", "later"));
    expect(log.read({ kind: "console", since: empty.next, limit: 50 })).toMatchObject({ droppedBefore: null, moreBefore: false });
  });
});

describe("TabLog failure counting", () => {
  // 目的：失败数按落定时刻计；潜在缺陷：同一请求先 500、后响应体失败时，被连续两次页面结果重复计数。
  it("reports the same request as a new failure only once", () => {
    const log = new TabLog();
    log.requestStarted("api", { method: "GET", url: "http://a/api/me", resourceType: "Fetch", timestamp: 1 });
    log.responseReceived("api", 500, 1.1);
    expect(log.digest().newLogs.failedRequests).toBe(1);
    log.requestFailed("api", "net::ERR_CONTENT_LENGTH_MISMATCH", 1.2);
    expect(log.digest().newLogs.failedRequests).toBe(0);
  });
});
