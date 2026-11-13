import { afterEach, describe, expect, it, vi } from "vitest";

import type { BrowserTabSummary } from "@/electron/shared/host-contract";

// 截住面板对标签镜像的订阅，按宿主推送的样子手动触发。
const tabsChange = vi.hoisted(() => ({ listener: null as null | ((tabs: readonly BrowserTabSummary[], previous: readonly BrowserTabSummary[]) => void) }));
vi.mock("@/lib/browser/host-state-store", () => ({
  onBrowserTabsChange: (listener: NonNullable<typeof tabsChange.listener>) => {
    tabsChange.listener = listener;
    return () => {};
  },
}));

import { displayedTab, followTabChanges, revealBrowserTab, useBrowserPanelStore } from "@/lib/browser/panel-store";
import { useDockStore } from "@/lib/layout/dock-store";

function tab(tabId: string, sessionId = 7, openerId: string | null = null): BrowserTabSummary {
  return { tabId, sessionId, openerId, createdBy: "agent", url: "https://example.com", title: "", loading: false, crashed: false, dialog: null, agentBusy: false, operation: null };
}

afterEach(() => {
  useBrowserPanelStore.setState({ sessionId: null, selected: {} });
});

describe("displayedTab", () => {
  it("选中的标签还在就显示它，否则回落到最新的标签", () => {
    const tabs = [tab("t1"), tab("t2")];
    expect(displayedTab(tabs, "t1")?.tabId).toBe("t1");
    expect(displayedTab(tabs, "t9")?.tabId).toBe("t2");
    expect(displayedTab(tabs, undefined)?.tabId).toBe("t2");
    expect(displayedTab([], "t1")).toBeUndefined();
  });
});

describe("followTabChanges", () => {
  it("显示中的标签开出新标签时切过去，同一 Session 别的标签开出的不跟", () => {
    const before = [tab("t1"), tab("t2")];
    const selected = { 7: "t1" };
    expect(followTabChanges(selected, before, [...before, tab("t3", 7, "t1")])).toEqual({ 7: "t3" });
    expect(followTabChanges(selected, before, [...before, tab("t3", 7, "t2")])).toBe(selected);
  });

  it("没有选中时以回落显示的最新标签为准跟随", () => {
    const before = [tab("t1"), tab("t2")];
    expect(followTabChanges({}, before, [...before, tab("t3", 7, "t2")])).toEqual({ 7: "t3" });
  });

  // 潜在缺陷：用户手动切回来源标签后，已存在的弹窗标签每次推送都把面板拽走。
  it("只在新标签出现那一刻跟随：之后用户切回来源标签不再被拽走", () => {
    const tabs = [tab("t1"), tab("t3", 7, "t1")];
    const selected = { 7: "t1" };
    expect(followTabChanges(selected, tabs, tabs.map((item) => ({ ...item, title: "加载完" })))).toBe(selected);
  });

  it("显示中的标签关闭：来源还在回到来源，来源不在清掉选中、回落到最新", () => {
    const tabs = [tab("t1"), tab("t2"), tab("t3", 7, "t1")];
    expect(followTabChanges({ 7: "t3" }, tabs, [tab("t1"), tab("t2")])).toEqual({ 7: "t1" });
    expect(followTabChanges({ 7: "t3" }, tabs.slice(1), [tab("t2")])).toEqual({});
    expect(followTabChanges({ 7: "t2" }, tabs, [tab("t1"), tab("t3", 7, "t1")])).toEqual({});
  });

  it("关闭的不是显示中的标签时选中不变", () => {
    const tabs = [tab("t1"), tab("t2", 7, "t1")];
    const selected = { 7: "t1" };
    expect(followTabChanges(selected, tabs, [tab("t1")])).toBe(selected);
  });

  // 潜在缺陷：`focus` 先于镜像收到新标签时，同一次推送里的弹窗或关闭规则覆盖掉显式选择。
  it("刚 focus、这次推送才到的标签以显式选择为准", () => {
    const before = [tab("t1")];
    const selected = { 7: "t5" };
    expect(followTabChanges(selected, before, [tab("t1"), tab("t2", 7, "t1"), tab("t5")])).toBe(selected);
  });

  it("各 Session 独立判定", () => {
    const before = [tab("t1", 7), tab("t2", 8)];
    const after = [...before, tab("t3", 8, "t2")];
    expect(followTabChanges({ 7: "t1" }, before, after)).toEqual({ 7: "t1", 8: "t3" });
  });
});

describe("panel store 订阅标签镜像", () => {
  it("镜像替换时按前后列表更新选中，没有变化不写入", () => {
    const listener = tabsChange.listener!;
    const before = [tab("t1")];
    useBrowserPanelStore.getState().select(7, "t1");
    listener([...before, tab("t2", 7, "t1")], before);
    expect(useBrowserPanelStore.getState().selected).toEqual({ 7: "t2" });

    const state = useBrowserPanelStore.getState();
    listener([tab("t1"), { ...tab("t2", 7, "t1"), title: "x" }], [tab("t1"), tab("t2", 7, "t1")]);
    expect(useBrowserPanelStore.getState()).toBe(state);
  });
});

describe("revealBrowserTab", () => {
  // 目的：工具卡片、交接、链接都用同一个动作把标签摆到面前；潜在缺陷：某处只选中不显示面板，或只显示面板不切标签。
  it("selects the tab in its session and shows the browser panel", () => {
    useDockStore.setState({ panel: null });
    revealBrowserTab(9, "t4");
    expect(useBrowserPanelStore.getState()).toMatchObject({ sessionId: 9, selected: { 9: "t4" } });
    expect(useDockStore.getState().panel).toBe("browser");
  });
});

