import { afterEach, describe, expect, it, vi } from "vitest";

import type { BrowserTabSummary } from "@/electron/shared/host-contract";

// 截住提醒对标签镜像的订阅，按宿主推送的样子手动触发（首份镜像 previous 为 null）。
const tabsChange = vi.hoisted(() => ({ listener: null as null | ((tabs: readonly BrowserTabSummary[], previous: readonly BrowserTabSummary[] | null) => void) }));
vi.mock("@/lib/browser/host-state-store", () => ({
  onBrowserTabsChange: (listener: NonNullable<typeof tabsChange.listener>) => {
    tabsChange.listener = listener;
    return () => {};
  },
  useSessionBrowserTabs: () => [],
}));

import { browserBadge, followUnseenTabs, useUnseenAgentTabsStore } from "@/lib/browser/browser-badge";

function tab(tabId: string, overrides: Partial<BrowserTabSummary> = {}): BrowserTabSummary {
  return { tabId, sessionId: 7, displaySessionId: 7, openerId: null, createdBy: "agent", url: "https://example.com", title: "", loading: false, crashed: false, dialog: null, agentBusy: false, operation: null, ...overrides };
}

afterEach(() => useUnseenAgentTabsStore.setState({ bySession: {} }));

describe("followUnseenTabs", () => {
  it("首份镜像里早就开着的标签不算没看过：刷新页面不提醒", () => {
    const unseen = {};
    expect(followUnseenTabs(unseen, null, [tab("t1"), tab("t2")])).toBe(unseen);
  });

  it("新出现的 Agent 标签记为没看过，用户自己开的不算；没有变化时原样返回", () => {
    const before = [tab("t1")];
    const after = followUnseenTabs({}, before, [...before, tab("t2"), tab("t3", { createdBy: "user" }), tab("t4", { sessionId: 8 })]);
    expect(after).toEqual({ 7: ["t2"], 8: ["t4"] });
    const same = followUnseenTabs(after, [...before, tab("t2")], [tab("t1"), tab("t2", { title: "加载完" })]);
    expect(same).toEqual({ 7: ["t2"] });
    expect(followUnseenTabs(after, [tab("t2"), tab("t4", { sessionId: 8 })], [tab("t2"), tab("t4", { sessionId: 8 })])).toBe(after);
  });

  it("没看过的标签关掉就移除，空了的会话不留桶", () => {
    expect(followUnseenTabs({ 7: ["t2", "t3"] }, [tab("t2"), tab("t3")], [tab("t3")])).toEqual({ 7: ["t3"] });
    expect(followUnseenTabs({ 7: ["t2"] }, [tab("t2")], [])).toEqual({});
  });

  it("订阅镜像写入 store；markSeen 清掉这个会话", () => {
    tabsChange.listener?.([tab("t1")], null);
    tabsChange.listener?.([tab("t1"), tab("t2")], [tab("t1")]);
    expect(useUnseenAgentTabsStore.getState().bySession).toEqual({ 7: ["t2"] });
    useUnseenAgentTabsStore.getState().markSeen(7);
    expect(useUnseenAgentTabsStore.getState().bySession).toEqual({});
  });
});

describe("browserBadge", () => {
  const tabs = [tab("t1"), tab("t2")];
  const idle = { tabs, unseen: [] as string[], browsing: false, delegatedBrowsing: false, showing: false };

  it("正在用浏览器时呼吸，优先于没看过", () => {
    expect(browserBadge({ ...idle, unseen: ["t2"], browsing: true })).toEqual({ count: 2, tone: "browsing", hint: "Agent 正在使用浏览器" });
  });

  it("停手后有没看过的 Agent 标签就静止", () => {
    expect(browserBadge({ ...idle, unseen: ["t1", "t2"] })).toEqual({ count: 2, tone: "unseen", hint: "Agent 打开了 2 个标签页" });
  });

  it("面板正显示这个会话时不提醒它自己的标签；没有可提醒的也不提醒", () => {
    expect(browserBadge({ ...idle, unseen: ["t2"], browsing: true, showing: true }).tone).toBeNull();
    expect(browserBadge(idle)).toEqual({ count: 2, tone: null, hint: null });
  });

  // 目的：子 Agent 在用浏览器也算这个会话在用，提示与主 Agent 分开；潜在缺陷：子 Agent 忙了半天父会话入口不动，
  // 或面板开着时被一并压掉——子 Agent 的标签不在面板里，用户看不到。
  it("子 Agent 正在用时呼吸并单独说明；主 Agent 在用时只说主 Agent；面板开着也照常提醒", () => {
    expect(browserBadge({ ...idle, delegatedBrowsing: true })).toEqual({ count: 2, tone: "browsing", hint: "子 Agent 正在使用浏览器" });
    expect(browserBadge({ ...idle, browsing: true, delegatedBrowsing: true }).hint).toBe("Agent 正在使用浏览器");
    expect(browserBadge({ ...idle, browsing: true, delegatedBrowsing: true, showing: true }).hint).toBe("子 Agent 正在使用浏览器");
    expect(browserBadge({ ...idle, unseen: ["t1"], delegatedBrowsing: true }).hint).toBe("子 Agent 正在使用浏览器");
  });
});
