import { expect, test, type Page } from "@playwright/test";
import type { ContextMessage } from "../../lib/api/context-messages";
import { API_ORIGIN } from "./e2e-env";
const record = (
  sequence: number,
  patch: Partial<ContextMessage> = {},
): ContextMessage => ({
  id: sequence,
  task_id: 42,
  turn: 1,
  inherited: false,
  sequence,
  include_in_context: true,
  created_at: "2026-10-17T01:02:03.123Z",
  updated_at: "2026-10-17T01:02:04.456Z",
  role: "user",
  content: `消息 ${sequence}：检查上下文记录`,
  ...patch,
});
const TURN_RUN = {
  status: "completed",
  end_reason: "stop",
  error_code: null,
  usage: { input_tokens: 1200, output_tokens: 50, total_tokens: 1250 },
  started_at: "2026-10-17T01:02:00.000Z",
  updated_at: "2026-10-17T01:03:00.000Z",
};
const fixtures = () => [
  record(0, { role: "system", turn: 0, content: "你是本地编程助手。" }),
  record(1),
  record(2, {
    role: "assistant",
    content: "",
    reasoning_content: "仅在观测中展示的推理原文",
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
        json: { detail: "task or turn not found" },
      });
    // 按回合查询时附这一回合所属运行的状态事实；不带回合时是整个 Task 的记录。
    return route.fulfill({
      json: url.searchParams.get("turn")
        ? { messages, run: TURN_RUN }
        : { messages: [...messages, record(101, { turn: 2, content: "其他回合消息" })], run: null },
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
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await page.setViewportSize({ width: 1600, height: 1000 });
});
test("空状态、加载反馈与查询边界", async ({ page }) => {
  const api = await mock(page);
  await page.goto("/context-observer");
  await expect(
    page.getByText("选择一个任务（可选回合），开始检查上下文"),
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
  await page.goto("/context-observer?task_id=oops&turn=1");
  await expect(page.getByRole("alert")).toContainText("查询地址无效");
  await page.goto("/context-observer?turn=1");
  await expect(page.getByRole("alert")).toContainText("要同时给出 Task ID");
});
test("筛选、调用关联与范围内定位", async ({ page }) => {
  await mock(page);
  await page.goto("/context-observer?task_id=42&turn=1");
  await expect(page.getByTestId("context-turn-run")).toContainText("所属运行（整个运行共享）：completed · stop · 1250 tokens");
  await expect(page.getByRole("option", { name: "#101 User", exact: true })).toHaveCount(0);
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
  await page.goto("/context-observer?task_id=42&turn=1");
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
  await page.getByLabel("回合", { exact: true }).fill("99");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("读取失败");
  await expect(detail(page)).toHaveCount(0);
});
test("详情用量、JSON、查找与键盘浏览，窄窗口返回保位", async ({ page }) => {
  await mock(page);
  await page.goto("/context-observer?task_id=42&turn=1");
  await list(page).focus();
  await list(page).press("ArrowDown");
  await list(page).press("ArrowDown");
  await expect(detail(page).getByRole("heading")).toContainText("#2");
  await detail(page).getByRole("button", { name: "用量", exact: true }).click();
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
  // 窄窗口（≤1024px）侧栏隐藏，整个宽度都给主区；860 让结果区落在 900 以下的窄布局。
  await page.setViewportSize({ width: 860, height: 800 });
  await page.getByRole("option", { name: "#2 Assistant", exact: true }).click();
  await expect(detail(page)).toBeVisible();
  await expect(list(page)).toBeHidden();
  await detail(page)
    .getByRole("button", { name: "返回列表", exact: true })
    .click();
  await expect(
    page.getByRole("option", { name: "#2 Assistant", exact: true }),
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
    await page.goto("/context-observer?task_id=42&turn=1");
    await page.getByRole("option", { name: "#3 Tool", exact: true }).click();
    await expect(detail(page).locator(".cm-content")).toContainText(
      "只读原始内容",
    );
    await expect(page.locator(".backend-startup-screen")).toHaveCount(0);
    await page.screenshot({
      path: `/private/tmp/cosir-observer-${theme}-desktop.png`,
    });
    // 窄窗口（≤1024px）侧栏隐藏，整个宽度都给主区；860 让结果区落在 900 以下的窄布局。
    await page.setViewportSize({ width: 860, height: 800 });
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


test("推理原文在独立观测页可查看与复制", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await mock(page);
  await page.goto("/context-observer?task_id=42&turn=1");
  await page.getByRole("option", { name: "#2 Assistant", exact: true }).click();
  await detail(page).getByRole("button", { name: "思考内容", exact: true }).click();
  await expect(detail(page).locator(".cm-content")).toContainText("仅在观测中展示的推理原文");
  await detail(page).getByRole("button", { name: "复制思考内容", exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe("仅在观测中展示的推理原文");
});
