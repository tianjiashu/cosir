import { expect, test, type Page } from "@playwright/test";
import type { BrowserDialog, BrowserDownload, BrowserState, BrowserTabSummary } from "@/electron/shared/host-contract";
import { fakeBrowserCalls, installFakeDesktopHost } from "./fake-desktop-host";
import { API_ORIGIN as api } from "./e2e-env";

/**
 * 浏览器面板上「等你操作」的提示条与下载条：只验证渲染进程交给宿主的参数。宿主状态是静态替身，作答后不会
 * 推送新状态，真实的标签、弹窗与下载行为由桌面 E2E 覆盖。
 */

function tab(tabId: string, title: string, dialog: BrowserDialog | null, taskId = 100): BrowserTabSummary {
  return { tabId, taskId, createdBy: "agent", openerId: null, url: `https://example.com/${tabId}`, title, loading: false, crashed: false, dialog, agentBusy: false };
}

function download(id: string, taskId: number, filename: string): BrowserDownload {
  return { id, taskId, tabId: "t9", filename, path: `/ws/.cosir/downloads/${filename}`, state: "completed", stalled: false, receivedBytes: 10, totalBytes: 10 };
}

async function openBrowserPanel(page: Page, state: BrowserState, options: { failAnswer?: boolean } = {}) {
  await installFakeDesktopHost(page, { browser: { state, ...options } });
  await page.goto("/tasks/100");
  await page.getByRole("button", { name: /^浏览器，/ }).click();
}

test.beforeEach(async ({ request }) => {
  await request.post(`${api}/__test__/reset`);
  await request.post(`${api}/__test__/seed-task`, { data: { taskId: 100, title: "浏览器任务" } });
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

test("下载条只显示面板所在 Task 的下载（触发标签已关闭也在），按下载 id 在文件夹中显示", async ({ page }) => {
  await openBrowserPanel(page, {
    tabs: [tab("t2", "别的任务", null, 101)],
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

