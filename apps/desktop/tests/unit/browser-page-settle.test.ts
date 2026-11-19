import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { untilPageSettled } from "@/electron/main/browser/page-loading";
import { TabLog } from "@/electron/main/browser/tab-log";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

/** 页面替身：`page` 是当前大纲（字符串代替），`pending` 是在途的内容请求；比对按字符串相等。 */
function fakePage(initial: string) {
  const state = { page: initial as string | null, pending: [] as string[], reads: 0 };
  const probe = {
    pending: () => state.pending,
    read: async () => { state.reads += 1; return state.page; },
    changed: (previous: string, current: string) => (previous === current ? { count: 0, example: null } : { count: 1, example: current }),
  };
  return { state, probe };
}
const options = (timeoutMs: number, until = new AbortController().signal) => ({ intervalMs: 300, timeoutMs, until });

describe("untilPageSettled", () => {
  // 目的：慢接口期间骨架屏是静止的；潜在缺陷：只看页面会在数据到达前就放行（实测搜索接口 4.7 秒）。
  it("keeps waiting while a content request is out, then needs the page to hold still", async () => {
    const { state, probe } = fakePage("skeleton");
    state.pending = ["search"];
    const settled = untilPageSettled(probe, options(3_000));
    await vi.advanceTimersByTimeAsync(1_000);
    expect(state.reads).toBe(0);
    state.pending = [];
    state.page = "results";
    await vi.advanceTimersByTimeAsync(400);
    await expect(settled).resolves.toMatchObject({ by: "stable", reads: 2 });
  });

  // 目的：WebSocket 推送、分批渲染没有请求可看；潜在缺陷：只看网络会把渲染到一半的页面交出去。
  it("waits for a page that is still rendering without requests", async () => {
    const { state, probe } = fakePage("part 1");
    const settled = untilPageSettled(probe, options(3_000));
    await vi.advanceTimersByTimeAsync(10);
    state.page = "part 2";
    await vi.advanceTimersByTimeAsync(300);
    state.page = "part 3";
    await vi.advanceTimersByTimeAsync(700);
    await expect(settled).resolves.toMatchObject({ by: "stable" });
    expect(state.reads).toBeGreaterThanOrEqual(4);
  });

  // 目的：计时器、轮播永远在变；潜在缺陷：死等，或交出时不说明是「页面一直在变」。
  it("gives up on a page that keeps changing and reports how", async () => {
    const { state, probe } = fakePage("tick 0");
    let tick = 0;
    const timer = setInterval(() => { state.page = `tick ${++tick}`; }, 100);
    const settled = untilPageSettled(probe, options(2_000));
    await vi.advanceTimersByTimeAsync(2_400);
    clearInterval(timer);
    await expect(settled).resolves.toMatchObject({ by: "changing", changed: 1, example: expect.stringMatching(/^tick /) });
  });

  // 目的：内容接口一直不回来；潜在缺陷：等满方法时限，或交出时不说是哪个请求。
  it("gives up at the limit and reports the content requests still out", async () => {
    const { state, probe } = fakePage("skeleton");
    state.pending = ["long-poll"];
    const settled = untilPageSettled(probe, options(2_000));
    await vi.advanceTimersByTimeAsync(2_100);
    await expect(settled).resolves.toMatchObject({ by: "requests", pending: ["long-poll"] });
  });

  // 目的：弹窗挡住时读不了大纲；潜在缺陷：在读不了的页面上空等满上限。
  it("stops when the page cannot be read or the wait is aborted", async () => {
    const unreadable = fakePage("x");
    unreadable.state.page = null;
    await expect(untilPageSettled(unreadable.probe, options(2_000))).resolves.toMatchObject({ by: "unreadable", reads: 1 });

    const abort = new AbortController();
    const { state, probe } = fakePage("x");
    state.pending = ["search"];
    const settled = untilPageSettled(probe, options(3_000, abort.signal));
    await vi.advanceTimersByTimeAsync(150);
    abort.abort();
    await vi.advanceTimersByTimeAsync(0);
    await expect(settled).resolves.toMatchObject({ by: "aborted" });
  });
});

describe("untilPageSettled evidence", () => {
  // 目的：请求回来之后页面早已停住；潜在缺陷（实测：快照里已有新内容，备注却说「还在变，例如 tick 8」）：
  // 请求前看到的变化在比对基准作废后仍被当成「还在变」的证据。
  it("drops change evidence from before a content request came back", async () => {
    const { state, probe } = fakePage("tick 1");
    const settled = untilPageSettled(probe, options(2_000));
    await vi.advanceTimersByTimeAsync(10);
    state.page = "tick 2";
    await vi.advanceTimersByTimeAsync(300);
    state.pending = ["slow"];
    await vi.advanceTimersByTimeAsync(1_650);
    state.pending = [];
    state.page = "loaded";
    await vi.advanceTimersByTimeAsync(400);
    await expect(settled).resolves.toMatchObject({ by: "stable" });
  });
});

describe("TabLog.pendingRequests", () => {
  const start = (log: TabLog, id: string, url: string, resourceType = "XHR", byPage = false, timestamp = 1, postData?: string) =>
    log.requestStarted(id, { method: postData === undefined ? "GET" : "POST", url, resourceType, timestamp, postData }, byPage);
  const graphql = (log: TabLog, id: string, operationName: string, timestamp = 1, byPage = false) =>
    start(log, id, "https://a.test/graphql", "Fetch", byPage, timestamp, JSON.stringify({ operationName, query: "{ x }" }));
  const urls = (log: TabLog, from: number) => log.pendingRequests(from).waiting.map((entry) => entry.url);

  // 目的：只等这一步带来的数据请求；潜在缺陷：之前就在跑的请求、图片或流式请求让每一步都卡满上限。
  it("counts only data requests started since the mark that are still running", () => {
    const log = new TabLog();
    start(log, "poll", "https://a.test/poll");
    const mark = log.nextSeq;
    start(log, "api", "https://a.test/api/search", "Fetch");
    start(log, "doc", "https://a.test/", "Document");
    start(log, "img", "https://cdn.test/a.png", "Image");
    start(log, "sse", "https://a.test/events", "EventSource");
    expect(urls(log, mark)).toEqual(["https://a.test/api/search", "https://a.test/"]);
    log.requestFinished("api", 3, 10);
    log.requestFailed("doc", "net::ERR_FAILED", 3);
    expect(log.pendingRequests(mark)).toEqual({ waiting: [], unwaited: [] });
  });

  // 目的：埋点、轮询与操作无关（实测小红书 collect 8 秒 22 次、轮询图片 50 秒 36 次，大小从 41 字节到 41KB 不等）；
  // 潜在缺陷：每步都等满上限、备注全是上报地址。按发起者认，与大小无关。
  it("stops waiting for an endpoint once the page has fetched it on its own repeatedly", () => {
    const log = new TabLog();
    start(log, "p1", "https://t.test/poll.png?t=1", "XHR", true);
    log.requestFinished("p1", 2, 42_000);
    start(log, "p2", "https://t.test/poll.png?t=2", "XHR", true, 3);
    log.requestFinished("p2", 4, 42_000);
    const mark = log.nextSeq;
    start(log, "p3", "https://t.test/poll.png?t=3");
    start(log, "api", "https://a.test/api/notes");
    expect(urls(log, mark)).toEqual(["https://a.test/api/notes"]);
    expect(log.pendingRequests(mark).unwaited.map((entry) => entry.url)).toEqual(["https://t.test/poll.png?t=3"]);
  });

  // 目的：页面空闲时只请求过一次的接口（懒加载、一次性的查询）与埋点不同；潜在缺陷（实测 GraphQL 页面）：一次空闲请求
  // 就让这个接口在本页面上永远不等，之后点击带来的内容都不等就交出。
  it("keeps waiting for an endpoint the page fetched on its own only once", () => {
    const log = new TabLog();
    start(log, "idle", "https://a.test/api/feed", "Fetch", true);
    log.requestFinished("idle", 2, 10);
    const mark = log.nextSeq;
    start(log, "click", "https://a.test/api/feed", "Fetch", false, 5);
    expect(urls(log, mark)).toEqual(["https://a.test/api/feed"]);
  });

  // 目的：刚打开的新页面上，埋点还没机会在操作之间被观察到；潜在缺陷：第一步照样被持续上报拖满上限。
  it("treats an endpoint fetched again later within one step as background", () => {
    const log = new TabLog();
    const mark = log.nextSeq;
    start(log, "c1", "https://t.test/collect?n=1");
    expect(urls(log, mark)).toEqual(["https://t.test/collect?n=1"]);
    start(log, "c2", "https://t.test/collect?n=2", "XHR", false, 1.15);
    expect(urls(log, mark)).toEqual([]);
    // 同一请求的重定向跳不是重复：/old 跳到 /new 仍是一个请求。
    start(log, "r", "https://a.test/api/item");
    log.responseReceived("r", 302, 1.1, true);
    start(log, "r", "https://a.test/api/item");
    expect(urls(log, mark)).toEqual(["https://a.test/api/item"]);
  });

  // 目的：一次点击同时发出几条请求（扇出）是这个动作要的内容；潜在缺陷（实测：两条 1.2 秒的查询，615ms 就交出）：
  // 同一时刻的几条被当成「重复」而全都不等。
  it("waits for requests to one endpoint fired together by one action", () => {
    const log = new TabLog();
    const mark = log.nextSeq;
    start(log, "q1", "https://a.test/api/batch", "Fetch", false, 1);
    start(log, "q2", "https://a.test/api/batch", "Fetch", false, 1.001);
    expect(urls(log, mark)).toEqual(["https://a.test/api/batch", "https://a.test/api/batch"]);
  });

  // 目的：GraphQL 的所有操作走同一路径，操作名才是接口；潜在缺陷：页面轮询的查询与点击带来的内容查询混成一个接口，
  // 轮询一被认出，内容查询也不等。
  it("tells GraphQL operations apart by operation name", () => {
    const log = new TabLog();
    graphql(log, "n1", "Notifications", 1, true);
    log.requestFinished("n1", 1.1, 10);
    graphql(log, "n2", "Notifications", 3, true);
    log.requestFinished("n2", 3.1, 10);
    const mark = log.nextSeq;
    graphql(log, "n3", "Notifications", 5);
    graphql(log, "feed", "Feed", 5.2);
    const { waiting, unwaited } = log.pendingRequests(mark);
    expect(waiting.map((entry) => log.endpoint(entry))).toEqual(["https://a.test/graphql (Feed)"]);
    expect(unwaited.map((entry) => log.endpoint(entry))).toEqual(["https://a.test/graphql (Notifications)"]);
    // 不像 GraphQL 的请求体、GET 持久化查询的操作名在查询参数里。
    start(log, "plain", "https://a.test/graphql", "Fetch", false, 6, "not json operationName");
    start(log, "persisted", "https://a.test/graphql?operationName=Feed&variables=%7B%7D", "Fetch", false, 6);
    const later = log.pendingRequests(mark);
    const endpointOfUrl = (fragment: string) => log.endpoint([...later.waiting, ...later.unwaited].find((entry) => entry.url.includes(fragment))!);
    expect(endpointOfUrl("/graphql")).toBe("https://a.test/graphql");
    expect(endpointOfUrl("operationName=Feed")).toBe("https://a.test/graphql (Feed)");
  });

  // 目的：翻页每次滚动都请求同一个接口，这正是该等的内容；潜在缺陷：同页反复出现就不等，滚动后快照里没有新内容。
  it("waits for the same content endpoint again in every step", () => {
    const log = new TabLog();
    start(log, "page1", "https://a.test/api/notes?page=1");
    log.requestFinished("page1", 2, 10);
    const step2 = log.nextSeq;
    start(log, "page2", "https://a.test/api/notes?page=2");
    expect(urls(log, step2)).toEqual(["https://a.test/api/notes?page=2"]);
    log.requestFinished("page2", 3, 10);
    const step3 = log.nextSeq;
    start(log, "page3", "https://a.test/api/notes?page=3");
    expect(urls(log, step3)).toEqual(["https://a.test/api/notes?page=3"]);
  });

  // 目的：后台事实属于当前页面；潜在缺陷：上一个页面的轮询接口在新页面上被当成内容接口以外的东西，或反过来一直不等。
  it("forgets background endpoints when the page changes", () => {
    const log = new TabLog();
    start(log, "b", "https://a.test/api/feed", "XHR", true);
    log.navigationStarted();
    log.pageStarted();
    const mark = log.nextSeq;
    start(log, "f", "https://a.test/api/feed");
    expect(urls(log, mark)).toEqual(["https://a.test/api/feed"]);
  });
});
