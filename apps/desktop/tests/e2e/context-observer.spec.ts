import { expect, test, type Page } from "@playwright/test";
import type { ContextMessage } from "../../lib/api/context-messages";
const record = (
  sequence: number,
  patch: Partial<ContextMessage> = {},
): ContextMessage => ({
  id: sequence,
  task_id: 42,
  run_id: 21,
  sequence,
  include_in_context: true,
  created_at: "2026-10-17T01:02:03.123Z",
  updated_at: "2026-10-17T01:02:04.456Z",
  role: "user",
  content: `消息 ${sequence}：检查上下文记录`,
  ...patch,
});
const fixtures = () => [
  record(0, { role: "system", run_id: null, content: "你是本地编程助手。" }),
  record(1),
  record(2, {
    role: "assistant",
    content: "",
    tool_calls: [
      { id: "call-1", name: "read_file", args: { path: "src/main.py" } },
    ],
    usage_metadata: {
      input_tokens: 1200,
      output_tokens: 50,
      total_tokens: 1250,
      input_token_details: { cache_read: 900 },
    },
  }),
  record(3, {
    role: "tool",
    tool_call_id: "call-1",
    status: "completed",
    content: Array.from(
      { length: 150 },
      (_, i) => `line ${i}: def greet_${i}(): return '只读原始内容'`,
    ).join("\n"),
  }),
  record(4, {
    role: "assistant",
    include_in_context: false,
    content: "尚未纳入的草稿记录",
  }),
  ...Array.from({ length: 95 }, (_, i) => record(i + 5)),
];
async function mock(page: Page) {
  let messages = fixtures();
  let fail = false;
  let gate: Promise<void> | null = null;
  let release: (() => void) | undefined;
  const requests: URL[] = [];
  await page.route("**/context-messages?*", async (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    if (gate) await gate;
    if (fail)
      return route.fulfill({
        status: 404,
        json: { detail: "task or run not found" },
      });
    return route.fulfill({
      json: url.searchParams.get("run_id")
        ? messages
        : [...messages, record(101, { run_id: 22, content: "其他轮次消息" })],
    });
  });
  return {
    requests,
    append: () => {
      messages = [...messages, record(100, { content: "刷新新增记录" })];
    },
    fail: (value: boolean) => {
      fail = value;
    },
    hold: () => {
      gate = new Promise<void>((resolve) => {
        release = resolve;
      });
    },
    release: () => {
      release?.();
      gate = null;
    },
  };
}
const detail = (page: Page) =>
  page.getByRole("region", { name: "消息详情", exact: true });
const list = (page: Page) =>
  page.getByRole("listbox", { name: "消息序列", exact: true });
test.beforeEach(async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await page.setViewportSize({ width: 1600, height: 1000 });
});
test("空状态、加载反馈与查询边界", async ({ page }) => {
  const api = await mock(page);
  await page.goto("/context-observer");
  await expect(
    page.getByText("选择一个任务或轮次，开始检查上下文"),
  ).toBeVisible();
  await expect(list(page)).toHaveCount(0);
  await page.getByLabel("Task ID", { exact: true }).fill("1.5");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("正整数");
  expect(api.requests.length).toBe(0);
  api.hold();
  await page.getByLabel("Task ID", { exact: true }).fill("42");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await expect(page.getByLabel("正在加载消息")).toBeVisible();
  api.release();
  await expect(list(page)).toBeVisible();
  await page.goto("/context-observer?task_id=oops&run_id=21");
  await expect(page.getByRole("alert")).toContainText("查询地址中的 ID 无效");
});
test("筛选、调用关联与范围内定位", async ({ page }) => {
  await mock(page);
  await page.goto("/context-observer?run_id=21");
  await expect(
    page.getByText("本轮记录，含系统提示词", { exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("按角色筛选").selectOption("assistant");
  await page.getByLabel("搜索已加载消息").fill("read_file");
  await page.getByRole("option", { name: "#2 Assistant", exact: true }).click();
  await detail(page)
    .getByRole("button", { name: "工具调用", exact: true })
    .click();
  await detail(page)
    .getByRole("button", { name: "查看结果 #3", exact: true })
    .click();
  await expect(detail(page).getByRole("heading")).toContainText("#3");
  await page.getByRole("button", { name: "返回筛选结果", exact: true }).click();
  await expect(page.getByLabel("搜索已加载消息")).toHaveValue("read_file");
  await expect(page.getByLabel("按角色筛选")).toHaveValue("assistant");
  await expect(detail(page).getByRole("heading")).toContainText("#2");
  await page.getByLabel("搜索已加载消息").fill("");
  await page.getByLabel("按纳入范围筛选").selectOption("excluded");
  await expect(
    page.getByRole("option", { name: "#4 Assistant", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "在全部记录中定位", exact: true }).click();
  await expect(detail(page).getByRole("heading")).toContainText("#4");
  await expect(page.getByRole("button", { name: "返回筛选结果" })).toBeVisible();
});
test("刷新保留列表与正文位置，失败保留旧结果，新范围不混入旧结果", async ({
  page,
}) => {
  const api = await mock(page);
  await page.goto("/context-observer?task_id=42&run_id=21");
  await page.getByRole("option", { name: "#3 Tool", exact: true }).click();
  const editor = detail(page).locator(".cm-scroller");
  await editor.evaluate((node) => {
    node.scrollTop = 400;
  });
  await list(page).evaluate((node) => {
    node.scrollTop = 500;
  });
  const top = await list(page).evaluate((node) => node.scrollTop);
  api.append();
  api.hold();
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(detail(page).getByRole("heading")).toContainText("#3");
  await expect(
    page.getByRole("button", { name: "正在读取", exact: true }),
  ).toBeDisabled();
  api.release();
  await expect(
    page.getByRole("button", { name: "新增 1 条 · 查看最新" }),
  ).toBeVisible();
  expect(
    Math.abs((await list(page).evaluate((node) => node.scrollTop)) - top),
  ).toBeLessThan(3);
  expect(await editor.evaluate((node) => node.scrollTop)).toBeGreaterThan(350);
  api.fail(true);
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "刷新失败，当前为上次读取结果",
  );
  await expect(detail(page).getByRole("heading")).toContainText("#3");
  api.fail(false);
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: "新增 1 条 · 查看最新" }).click();
  await expect(detail(page).getByRole("heading")).toContainText("#100");
  api.fail(true);
  await page.getByLabel("Run ID", { exact: true }).fill("99");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("读取失败");
  await expect(detail(page)).toHaveCount(0);
});
test("详情用量、JSON、查找与键盘浏览，窄窗口返回保位", async ({ page }) => {
  await mock(page);
  await page.goto("/context-observer?run_id=21");
  await list(page).focus();
  await list(page).press("ArrowDown");
  await list(page).press("ArrowDown");
  await expect(detail(page).getByRole("heading")).toContainText("#2");
  await detail(page).getByRole("button", { name: "用量", exact: true }).click();
  await expect(detail(page)).toContainText("缓存命中率 75%");
  await detail(page).getByRole("button", { name: "JSON", exact: true }).click();
  await expect(detail(page).locator(".cm-content")).toHaveAttribute(
    "contenteditable",
    "false",
  );
  await expect(detail(page).locator(".cm-content")).toContainText(
    '"tool_calls"',
  );
  await detail(page).locator(".cm-content").focus();
  await page.keyboard.press(
    process.platform === "darwin" ? "Meta+f" : "Control+f",
  );
  await expect(detail(page).locator(".cm-search")).toBeVisible();
  await page.keyboard.press("Escape");
  const handle = page.getByRole("separator", { name: "调整消息列表宽度" });
  await handle.focus();
  const before = Number(await handle.getAttribute("aria-valuenow"));
  await page.keyboard.press("ArrowRight");
  expect(Number(await handle.getAttribute("aria-valuenow"))).toBe(before + 24);
  await page.setViewportSize({ width: 900, height: 800 });
  await page.getByRole("option", { name: "#2 Assistant", exact: true }).click();
  await expect(detail(page)).toBeVisible();
  await expect(list(page)).toBeHidden();
  await detail(page)
    .getByRole("button", { name: "返回列表", exact: true })
    .click();
  await expect(
    page.getByRole("option", { name: "#80 User", exact: true }),
  ).toBeVisible();
  await expect(list(page)).toBeFocused();
});
for (const theme of ["light", "dark"] as const) {
  test(`${theme} 桌面与窄窗口布局`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(
      (value) => localStorage.setItem("cosir.theme-preference", value),
      theme,
    );
    await mock(page);
    await page.goto("/context-observer?task_id=42&run_id=21");
    await page.getByRole("option", { name: "#3 Tool", exact: true }).click();
    await expect(detail(page).locator(".cm-content")).toContainText(
      "只读原始内容",
    );
    await expect(page.locator(".backend-startup-screen")).toHaveCount(0);
    await page.screenshot({
      path: `/private/tmp/cosir-observer-${theme}-desktop.png`,
    });
    await page.setViewportSize({ width: 900, height: 800 });
    await expect(
      detail(page).getByRole("button", { name: "返回列表", exact: true }),
    ).toBeVisible();
    await page.screenshot({
      path: `/private/tmp/cosir-observer-${theme}-narrow.png`,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    expect(errors).toEqual([]);
  });
}
