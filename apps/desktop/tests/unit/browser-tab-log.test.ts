import { describe, expect, it } from "vitest";

import { BODY_LIMITS, CANCELED_FAILURE, TabLog } from "@/electron/main/browser/tab-log";

const consoleEntry = (level: "debug" | "info" | "warning" | "error", text: string) => ({ level, text, source: null, frame: null });

describe("TabLog", () => {
  // 目的：「上次以来」只在页面结果处消费；潜在缺陷：只为拿标签 id 的调用吞掉了下载完成之类的备注。
  it("hands notes and new-log counts to page results only once, and reading logs consumes nothing", () => {
    const log = new TabLog();
    log.note("a download started");
    log.console(consoleEntry("error", "boom"));
    log.console(consoleEntry("warning", "careful"));
    log.requestStarted("r1", { method: "GET", url: "http://a/x", resourceType: "Fetch", timestamp: 1 });
    log.responseReceived("r1", 500, 1.02);
    log.requestFinished("r1", 1.03, 10);
    expect(log.undeliveredNotes()).toEqual(["a download started"]);
    expect(log.read({ kind: "console", limit: 50 }).entries).toHaveLength(2);

    expect(log.digest()).toEqual({ notes: ["a download started"], newLogs: { consoleErrors: 1, consoleWarnings: 1, failedRequests: 1 } });
    expect(log.digest()).toEqual({ notes: [], newLogs: { consoleErrors: 0, consoleWarnings: 0, failedRequests: 0 } });
    expect(log.undeliveredNotes()).toEqual([]);
    expect(log.read({ kind: "network", failedOnly: true, limit: 50 }).entries).toEqual([
      { seq: 4, method: "GET", url: "http://a/x", status: 500, resourceType: "Fetch", failure: null, durationMs: 30, body: { state: "pending", mime: null, size: null } },
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
    log.requestFinished("doc", 1.02, 10);
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
    log.requestFinished("r1", 10.25, 10);
    log.requestStarted("r2", { method: "POST", url: "http://a/api", resourceType: "XHR", timestamp: 11 });
    log.requestFailed("r2", "net::ERR_CONNECTION_REFUSED", 11.5);
    expect(log.read({ kind: "network", failedOnly: false, limit: 50 }).entries).toEqual([
      { seq: 1, method: "GET", url: "http://a/old", status: 302, resourceType: "Document", failure: null, durationMs: 100, body: null },
      { seq: 2, method: "GET", url: "http://a/new", status: 200, resourceType: "Document", failure: null, durationMs: 150, body: null },
      { seq: 3, method: "POST", url: "http://a/api", status: null, resourceType: "XHR", failure: "net::ERR_CONNECTION_REFUSED", durationMs: 500, body: null },
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

describe("TabLog response bodies", () => {
  const fetchStarted = (log: TabLog, id: string, type = "Fetch") => log.requestStarted(id, { method: "POST", url: `http://a/${id}`, resourceType: type, timestamp: 1 });
  /** 一条请求从开始到采下正文；返回它的序号。 */
  const captured = (log: TabLog, id: string, text: string, mime = "application/json") => {
    fetchStarted(log, id);
    log.responseReceived(id, 200, 1.1, false, mime);
    const seq = log.requestFinished(id, 1.2, Buffer.byteLength(text));
    expect(seq).not.toBeNull();
    log.bodyCaptured(seq!, text);
    return seq!;
  };

  // 目的：核心场景「保存失败」要看得到接口返回了什么；潜在缺陷：只采 2xx，或采下后改动了条目原有字段。
  it("keeps a 500 JSON body on its entry without touching the entry or the failure count", () => {
    const log = new TabLog();
    fetchStarted(log, "save");
    log.responseReceived("save", 500, 1.1, false, "application/json; charset=utf-8");
    const seq = log.requestFinished("save", 1.2, 30)!;
    log.bodyCaptured(seq, '{"error":"quota exceeded"}');
    const read = log.readBody(seq)!;
    expect(read.entry).toMatchObject({ seq, status: 500, durationMs: 200, resourceType: "Fetch" });
    expect(read.body).toEqual({ state: "text", mime: "application/json; charset=utf-8", size: 26, text: '{"error":"quota exceeded"}', truncated: false, reason: null });
    expect(log.digest().newLogs.failedRequests).toBe(1);
  });

  // 目的：宿主单条上限按 UTF-8 字节截断；潜在缺陷：按 UTF-16 字符计，或截断切坏多字节字符。
  it("truncates a body over the stored limit by UTF-8 bytes without splitting characters", () => {
    const log = new TabLog();
    const exact = "a".repeat(BODY_LIMITS.storedBodyBytes);
    expect(log.readBody(captured(log, "exact", exact))!.body).toMatchObject({ truncated: false, size: BODY_LIMITS.storedBodyBytes });
    const wide = "中".repeat(Math.ceil(BODY_LIMITS.storedBodyBytes / 3));
    const body = log.readBody(captured(log, "wide", wide))!.body;
    expect(body.truncated).toBe(true);
    expect(body.size).toBe(Buffer.byteLength(wide));
    expect(Buffer.byteLength(body.text!)).toBeLessThanOrEqual(BODY_LIMITS.storedBodyBytes);
    expect(body.text!.endsWith("中")).toBe(true);
  });

  // 目的：每标签总量有上限、条目保留；潜在缺陷：挤出后字节计数漂移，之后的正文被错误挤掉或无限增长。
  it("evicts the oldest bodies past the per-tab total and keeps the entries", () => {
    const log = new TabLog();
    const chunk = "x".repeat(BODY_LIMITS.storedBodyBytes);
    const count = BODY_LIMITS.storedTotalBytes / BODY_LIMITS.storedBodyBytes;
    const seqs = Array.from({ length: count + 2 }, (_, index) => captured(log, `b${index}`, chunk));
    expect(log.readBody(seqs[0])!.body).toMatchObject({ state: "evicted", text: null });
    expect(log.readBody(seqs[1])!.body).toMatchObject({ state: "evicted", text: null });
    for (const seq of seqs.slice(2)) expect(log.readBody(seq)!.body.state).toBe("text");
    // 再采一条只挤掉一条：计数没有漂移。
    const next = captured(log, "next", chunk);
    expect(log.readBody(seqs[2])!.body.state).toBe("evicted");
    expect(log.readBody(seqs[3])!.body.state).toBe("text");
    expect(log.readBody(next)!.body.state).toBe("text");
  });

  // 目的：正文与条目同生共死；潜在缺陷：条目被 1000 条容量挤掉后正文仍占着总量，新正文被误挤。
  it("releases a body with its entry and reports a dropped entry as missing", () => {
    const log = new TabLog();
    const chunk = "x".repeat(BODY_LIMITS.storedBodyBytes);
    const first = captured(log, "first", chunk);
    for (let index = 0; index < 1000; index += 1) fetchStarted(log, `r${index}`, "Document");
    expect(log.readBody(first)).toBeNull();
    const seqs = Array.from({ length: BODY_LIMITS.storedTotalBytes / BODY_LIMITS.storedBodyBytes }, (_, index) => captured(log, `c${index}`, chunk));
    for (const seq of seqs) expect(log.readBody(seq)!.body.state).toBe("text");
  });

  // 目的：重定向的中间跳没有正文；潜在缺陷：同一 requestId 的正文串到上一跳。
  it("gives only the last redirect hop a body", () => {
    const log = new TabLog();
    fetchStarted(log, "r");
    log.responseReceived("r", 302, 1.05, true);
    fetchStarted(log, "r");
    log.responseReceived("r", 200, 1.1, false, "application/json");
    const seq = log.requestFinished("r", 1.2, 2)!;
    log.bodyCaptured(seq, "{}");
    const entries = log.read({ kind: "network", failedOnly: false, limit: 50 }).entries;
    expect(entries.map((entry) => entry.body?.state ?? null)).toEqual([null, "text"]);
  });

  // 目的：状态表的每一种都有明确表示；潜在缺陷：没有正文的情形一律显示成空，Agent 分不清「空」与「没采到」。
  it("labels binary, streaming, empty, too large, failed and non-Fetch requests", () => {
    const log = new TabLog();
    fetchStarted(log, "png");
    log.responseReceived("png", 200, 1.1, false, "image/png");
    expect(log.requestFinished("png", 1.2, 4096)).toBeNull();
    fetchStarted(log, "sse", "EventSource");
    const empty = captured(log, "empty", "");
    fetchStarted(log, "big");
    log.responseReceived("big", 200, 1.1, false, "application/json");
    expect(log.requestFinished("big", 1.2, BODY_LIMITS.rendererResourceBytes + 1)).toBeNull();
    fetchStarted(log, "down");
    log.requestFailed("down", "net::ERR_CONNECTION_REFUSED", 1.2);
    log.requestStarted("doc", { method: "GET", url: "http://a/", resourceType: "Document", timestamp: 1 });
    fetchStarted(log, "flight");
    const states = log.read({ kind: "network", failedOnly: false, limit: 50 }).entries.map((entry) => entry.body);
    expect(states).toEqual([
      { state: "binary", mime: "image/png", size: 4096 },
      { state: "streaming", mime: null, size: null },
      { state: "empty", mime: "application/json", size: 0 },
      { state: "too_large", mime: "application/json", size: BODY_LIMITS.rendererResourceBytes + 1 },
      null,
      null,
      { state: "pending", mime: null, size: null },
    ]);
    expect(log.readBody(empty)!.body).toMatchObject({ text: "", truncated: false });
    expect(log.readBody(5)!.body).toMatchObject({ state: "unavailable", text: null, reason: expect.stringContaining("failed") });
    expect(log.readBody(6)!.body).toMatchObject({ state: "unavailable", reason: expect.stringContaining("Document") });
    expect(log.readBody(7)!.body).toMatchObject({ state: "pending", text: null });
  });

  // 目的：正文跨导航可读、读取不消费；潜在缺陷：换页清空正文，或读正文推进了「上次以来」吞掉备注。
  it("keeps bodies across navigation and reading them consumes nothing", () => {
    const log = new TabLog();
    const seq = captured(log, "api", '{"ok":false}');
    log.note("download finished");
    log.navigationStarted();
    log.pageStarted();
    expect(log.readBody(seq)!.body.text).toBe('{"ok":false}');
    expect(log.read({ kind: "network", failedOnly: false, limit: 50 }).entries).toEqual([]);
    expect(log.undeliveredNotes()).toEqual(["download finished"]);
  });

  // 目的：找不到的序号要能分辨；潜在缺陷：console 的序号被当成网络请求读出空正文。
  it("finds no network request for unknown or console sequence numbers", () => {
    const log = new TabLog();
    log.console(consoleEntry("info", "hello"));
    expect(log.readBody(1)).toBeNull();
    expect(log.readBody(99)).toBeNull();
  });

  // 目的：被弹窗挡住的采集能补上；潜在缺陷：deferred 的条目没有留下 requestId，或补采结果写不回来。
  it("hands deferred captures back for retry and records the outcome", () => {
    const log = new TabLog();
    fetchStarted(log, "a");
    log.responseReceived("a", 500, 1.1, false, "application/json");
    const a = log.requestFinished("a", 1.2, 10)!;
    fetchStarted(log, "b");
    log.responseReceived("b", 200, 1.1, false, "application/json");
    const b = log.requestFinished("b", 1.2, 10)!;
    log.bodyDeferred(a);
    log.bodyDeferred(b);
    expect(log.readBody(a)!.body).toMatchObject({ state: "deferred", text: null, reason: expect.stringContaining("dialog") });
    expect(log.takeDeferred()).toEqual([{ seq: a, requestId: "a" }, { seq: b, requestId: "b" }]);
    expect(log.takeDeferred()).toEqual([]);
    log.bodyCaptured(a, '{"error":"x"}');
    log.bodyUnavailable(b, "the page moved on");
    expect(log.readBody(a)!.body.state).toBe("text");
    expect(log.readBody(b)!.body).toMatchObject({ state: "unavailable", reason: "the page moved on" });
    // 迟到的回执不改已定的状态。
    log.bodyCaptured(b, "late");
    expect(log.readBody(b)!.body.state).toBe("unavailable");
  });

  // 目的：截掉的部分不再保存，要让模型知道读不到全文是工具的上限；潜在缺陷：一个「…」看不出截了多少、是不是页面原文。
  it("marks how much of a long console message or address was cut", () => {
    const log = new TabLog();
    log.console(consoleEntry("error", "x".repeat(520)));
    log.requestStarted("r", { method: "GET", url: `https://a.test/${"p".repeat(600)}`, resourceType: "Fetch", timestamp: 1 });
    expect(log.read({ kind: "console", limit: 5 }).entries[0].text).toBe(`${"x".repeat(500)}…(+20 chars)`);
    expect(log.read({ kind: "network", failedOnly: false, limit: 5 }).entries[0].url).toMatch(/…\(\+115 chars\)$/);
  });
});
