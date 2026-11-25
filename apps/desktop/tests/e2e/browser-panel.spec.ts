import { expect, test, type Page } from "@playwright/test";
import type { BrowserDialog, BrowserDownload, BrowserState, BrowserTabSummary } from "@/electron/shared/host-contract";
import { fakeBrowserCalls, installFakeDesktopHost, pushBrowserState } from "./fake-desktop-host";
import { API_ORIGIN as api } from "./e2e-env";
import { completedState, seedState } from "./transport-fixtures";

/**
 * 浏览器面板上「等你操作」的提示条与下载条：只验证渲染进程交给宿主的参数。宿主状态是静态替身，作答后不会
 * 推送新状态，真实的标签、弹窗与下载行为由桌面 E2E 覆盖。
 */

function tab(tabId: string, title: string, dialog: BrowserDialog | null, sessionId = 100): BrowserTabSummary {
  return { tabId, sessionId, displaySessionId: sessionId, createdBy: "agent", openerId: null, url: `https://example.com/${tabId}`, title, loading: false, crashed: false, dialog, agentBusy: false, operation: null };
}

function download(id: string, sessionId: number, filename: string): BrowserDownload {
  return { id, sessionId, tabId: "t9", filename, path: `/ws/.cosir/downloads/${filename}`, state: "completed", stalled: false, receivedBytes: 10, totalBytes: 10 };
}

async function openBrowserPanel(page: Page, state: BrowserState, options: { failAnswer?: boolean } = {}) {
  await installFakeDesktopHost(page, { browser: { state, ...options } });
  await page.goto("/sessions/100");
  await page.getByRole("button", { name: /^浏览器，/ }).click();
}

test.beforeEach(async ({ request }) => {
  await request.post(`${api}/__test__/reset`);
  await request.post(`${api}/__test__/seed-session`, { data: { sessionId: 100, title: "浏览器会话" } });
});

test("弹窗提示条按弹窗 id 作答：prompt 接受带输入、取消只有 accept:false；confirm 可取消；alert 只有确定", async ({ page }) => {
  await openBrowserPanel(page, {
    tabs: [
      tab("t1", "提示页", { id: 11, type: "alert", message: "保存成功", defaultText: "" }),
      tab("t2", "确认页", { id: 12, type: "confirm", message: "确定删除？", defaultText: "" }),
      tab("t3", "输入页", { id: 13, type: "prompt", message: "你的名字", defaultText: "张三" }),
    ],
    downloads: [],
  });
  const banner = page.getByTestId("browser-dialog-banner");

  // 面板默认显示最新的标签（prompt）。
  await expect(banner.getByLabel("弹窗输入")).toHaveValue("张三");
  await banner.getByLabel("弹窗输入").fill("李四");
  await banner.getByRole("button", { name: "确定" }).click();
  await banner.getByRole("button", { name: "取消" }).click();

  await page.getByRole("tab", { name: "确认页" }).click();
  await expect(banner).toContainText("确定删除？");
  await banner.getByRole("button", { name: "取消" }).click();

  await page.getByRole("tab", { name: "提示页" }).click();
  await expect(banner).toContainText("保存成功");
  await expect(banner.getByRole("button", { name: "取消" })).toHaveCount(0);
  await banner.getByRole("button", { name: "确定" }).click();

  await expect.poll(() => fakeBrowserCalls(page)).toEqual([
    { method: "answerDialog", args: ["t3", 13, { accept: true, text: "李四" }] },
    { method: "answerDialog", args: ["t3", 13, { accept: false }] },
    { method: "answerDialog", args: ["t2", 12, { accept: false }] },
    { method: "answerDialog", args: ["t1", 11, { accept: true }] },
  ]);
});

test("作答失败时提示用户，提示条留着可以再答", async ({ page }) => {
  await openBrowserPanel(page, { tabs: [tab("t1", "确认页", { id: 5, type: "confirm", message: "继续？", defaultText: "" })], downloads: [] }, { failAnswer: true });
  const banner = page.getByTestId("browser-dialog-banner");
  await banner.getByRole("button", { name: "确定" }).click();
  await expect(page.getByText("回答网页弹窗失败")).toBeVisible();
  await expect(banner.getByRole("button", { name: "确定" })).toBeEnabled();
});

test("下载条只显示面板所在 Session 的下载（触发标签已关闭也在），按下载 id 在文件夹中显示", async ({ page }) => {
  await openBrowserPanel(page, {
    tabs: [tab("t2", "别的会话", null, 101)],
    downloads: [download("d1", 100, "report.pdf"), download("d2", 101, "other.zip")],
  });
  const bar = page.getByRole("region", { name: "下载" });
  await expect(bar).toContainText("report.pdf");
  await expect(bar).not.toContainText("other.zip");
  await expect(bar.getByRole("button", { name: /^全部/ })).toHaveCount(0);

  await bar.getByRole("button", { name: "在文件夹中显示 report.pdf" }).click();
  await expect.poll(() => fakeBrowserCalls(page)).toEqual([{ method: "revealDownload", args: ["d1"] }]);
});

// 目的：用户能看出页面变化是 Agent 在做；潜在缺陷：执行中的标签没有标记，或提示条盖过了待答的弹窗。
test("Agent 正在操作的标签有标记，网页上方显示提示；弹窗优先", async ({ page }) => {
  await openBrowserPanel(page, {
    tabs: [{ ...tab("t1", "弹窗页", { id: 3, type: "alert", message: "hi", defaultText: "" }), agentBusy: true }, { ...tab("t2", "商品页", null), agentBusy: true }],
    downloads: [],
  });
  await expect(page.getByRole("status").filter({ hasText: "Agent 正在操作这个页面" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "商品页" }).getByLabel("Agent 正在操作")).toBeVisible();
  await page.getByRole("tab", { name: "弹窗页" }).click();
  await expect(page.getByTestId("browser-dialog-banner")).toBeVisible();
  await expect(page.getByText("Agent 正在操作这个页面")).toHaveCount(0);
});


test("未知输入状态提示检查页面并保留关闭入口，不显示正在操作", async ({ page }) => {
  await openBrowserPanel(page, { tabs: [{ ...tab("t1", "待检查页", null), operation: "unknown" }], downloads: [] });
  await expect(page.getByRole("status").filter({ hasText: "页面操作尚未确认结束" })).toBeVisible();
  await expect(page.getByText("Agent 正在操作这个页面")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "关闭 待检查页" })).toBeEnabled();
});

test("面板在一次对话里新开标签后，切到别的会话仍跟随当前会话：显示它的标签、新标签开在它名下", async ({ page, request }) => {
  await request.post(`${api}/__test__/seed-session`, { data: { sessionId: 101, title: "另一个会话" } });
  await page.route("**/browser-context", (route) => route.fulfill({ json: { limits: { per_session: 4, total: 10 }, download_dir: "/tmp" } }));
  await openBrowserPanel(page, { tabs: [tab("t100", "甲的页面", null, 100), tab("t101", "乙的页面", null, 101)], downloads: [] });
  await expect(page.getByRole("tab", { name: "甲的页面" })).toBeVisible();
  const opened = async () => (await fakeBrowserCalls(page)).filter((call) => call.method === "browser.open").map((call) => call.args[0]);

  await page.getByRole("button", { name: "新标签页", exact: true }).click();
  await expect.poll(opened).toEqual([100]);
  await page.getByRole("treeitem", { name: "另一个会话", exact: true }).click();
  await expect(page).toHaveURL(/sessions\/101$/);
  await expect(page.getByRole("tab", { name: "乙的页面" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "甲的页面" })).toHaveCount(0);
  await page.getByRole("button", { name: "新标签页", exact: true }).click();
  await expect.poll(opened).toEqual([100, 101]);
});

test("工具卡片「打开」把面板定位到子 Agent 的标签：本次对话里一直有效，切到别的会话才回到跟随", async ({ page, request }) => {
  await request.post(`${api}/__test__/seed-session`, { data: { sessionId: 101, title: "另一个会话" } });
  await seedState(request, 100, completedState(3, { id: "command", parts: [{ type: "text", text: "看看子 Agent 的页面" }] }, [
    { presentation: "activity", type: "tool-call", toolCallId: "page-call", toolName: "browser_navigate", args: { url: "https://example.com/child" }, status: "completed", awaitingUser: false, display_data: { kind: "browser-page", tab_id: "t-child", url: "https://example.com/child", title: "子 Agent 的页面" } },
    { type: "text", text: "已打开。" },
  ] as never));
  await page.route("**/browser-context", (route) => route.fulfill({ json: { limits: { per_session: 4, total: 10 }, download_dir: "/tmp" } }));
  await openBrowserPanel(page, { tabs: [tab("t100", "甲的页面", null, 100), tab("t-child", "子 Agent 的页面", null, 500), tab("t101", "乙的页面", null, 101)], downloads: [] });
  await expect(page.getByRole("tab", { name: "甲的页面" })).toBeVisible();
  const opened = async () => (await fakeBrowserCalls(page)).filter((call) => call.method === "browser.open").map((call) => call.args[0]);

  await page.getByRole("button", { name: "打开网页 https://example.com/child" }).click();
  const open = page.getByRole("button", { name: "在浏览器面板打开：子 Agent 的页面" });
  await open.click({ force: true });
  await expect(page.getByRole("tab", { name: "子 Agent 的页面" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "甲的页面" })).toHaveCount(0);
  // 定位后在面板里新开标签：仍开在子 Agent 名下。
  await page.getByRole("button", { name: "新标签页", exact: true }).click();
  await expect.poll(opened).toEqual([500]);
  await expect(page.getByRole("tab", { name: "子 Agent 的页面" })).toBeVisible();

  await page.getByRole("treeitem", { name: "另一个会话", exact: true }).click();
  await expect(page.getByRole("tab", { name: "乙的页面" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "子 Agent 的页面" })).toHaveCount(0);
});

// 目的：「看过」按面板实际显示的会话算；潜在缺陷：只在面板显示当前对话时才记，从父会话的卡片看过子 Agent 的
// 标签后，子会话仍留着「Agent 打开了 1 个标签页」。
test("在父会话里经卡片看过子 Agent 新开的标签：子会话的「没看过」随即清掉", async ({ page, request }) => {
  await seedState(request, 100, completedState(3, { id: "command", parts: [{ type: "text", text: "看看子 Agent 的页面" }] }, [
    { presentation: "activity", type: "tool-call", toolCallId: "page-call", toolName: "browser_navigate", args: { url: "https://example.com/child" }, status: "completed", awaitingUser: false, display_data: { kind: "browser-page", tab_id: "t-child", url: "https://example.com/child", title: "子 Agent 的页面" } },
    { type: "text", text: "已打开。" },
  ] as never));
  const own = tab("t100", "甲的页面", null, 100);
  await installFakeDesktopHost(page, { browser: { state: { tabs: [own], downloads: [] } } });
  await page.goto("/sessions/100");
  const unseenOfChild = () => page.evaluate(async () => {
    const modulePath = "/lib/browser/browser-badge.ts";
    const { useUnseenAgentTabsStore } = await import(modulePath);
    return useUnseenAgentTabsStore.getState().bySession[500] ?? [];
  });
  await expect(page.getByRole("button", { name: /^浏览器，1 个标签页/ })).toBeVisible();

  // 子 Agent 新开了标签：经宿主推送出现，记为没看过。
  await pushBrowserState(page, { tabs: [own, tab("t-child", "子 Agent 的页面", null, 500)], downloads: [] });
  await expect.poll(unseenOfChild).toEqual(["t-child"]);

  await page.getByRole("button", { name: "打开网页 https://example.com/child" }).click();
  await page.getByRole("button", { name: "在浏览器面板打开：子 Agent 的页面" }).click({ force: true });
  await expect(page.getByRole("tab", { name: "子 Agent 的页面" })).toBeVisible();
  await expect.poll(unseenOfChild).toEqual([]);
});

// 目的：子 Agent 在用浏览器也算这个会话在用；潜在缺陷：父会话入口不动，或子 Agent / 分叉的标签混进面板与计数。
test("子 Agent 在用浏览器：入口呼吸并说「子 Agent」，标签数与面板只算自己的；分叉在用不算", async ({ page }) => {
  const own = tab("t100", "甲的页面", null, 100);
  await installFakeDesktopHost(page, { browser: { state: { tabs: [own], downloads: [] } } });
  await page.goto("/sessions/100");
  const globe = page.getByRole("button", { name: /^浏览器，/ });
  await expect(globe).toHaveAccessibleName(/^浏览器，1 个标签页/);

  // 分叉（显示在它自己页上）在用：当前会话不提醒。
  const fork = { ...tab("t-fork", "分叉的页面", null, 300), agentBusy: true };
  await pushBrowserState(page, { tabs: [own, fork], downloads: [] });
  await expect(globe).toHaveAccessibleName("浏览器，1 个标签页，点击打开");

  // 子 Agent（显示在当前会话页上）在用：呼吸，单独说明。
  const child = { ...tab("t-child", "子 Agent 的页面", null, 500), displaySessionId: 100, agentBusy: true };
  await pushBrowserState(page, { tabs: [own, fork, child], downloads: [] });
  await expect(globe).toHaveAccessibleName(/^浏览器，1 个标签页.*子 Agent 正在使用浏览器/);

  // 面板只显示自己的标签；子 Agent 的看不到，所以面板开着也照常提醒。
  await globe.click();
  await expect(page.getByRole("tab", { name: "甲的页面" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "子 Agent 的页面" })).toHaveCount(0);
  await expect(globe).toHaveAccessibleName(/子 Agent 正在使用浏览器/);
});

test("Agent 正在用浏览器时切到别的会话：那个会话的入口不沿用「正在使用」的保持期", async ({ page, request }) => {
  await request.post(`${api}/__test__/seed-session`, { data: { sessionId: 101, title: "另一个会话" } });
  await installFakeDesktopHost(page, { browser: { state: { tabs: [{ ...tab("t100", "甲的页面", null, 100), agentBusy: true }], downloads: [] } } });
  await page.goto("/sessions/100");
  const globe = page.getByRole("button", { name: /^浏览器，/ });
  await expect(globe).toHaveAccessibleName(/Agent 正在使用浏览器/);
  await page.getByRole("treeitem", { name: "另一个会话", exact: true }).click();
  await expect(page).toHaveURL(/sessions\/101$/);
  // 入口换成乙的标签数的当下就不该再说「正在使用」（保持期是 2 秒，等下去会掩盖问题）。
  await expect(globe).toHaveAccessibleName(/^浏览器，0 个标签页/);
  expect(await globe.getAttribute("aria-label")).not.toContain("Agent 正在使用浏览器");
  await page.getByRole("treeitem", { name: "浏览器会话", exact: true }).click();
  await expect(globe).toHaveAccessibleName(/Agent 正在使用浏览器/);
});
