import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { API_ORIGIN, OUTPUT_DIR } from "./e2e-env";
import { seedState } from "./transport-fixtures";
import { activity, assistantMessage, outcome, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";
import type { TransportToolDisplayData } from "../../lib/assistant/contract";

type Sample = { status: string; display: TransportToolDisplayData };
let samples: Record<string, Sample>;
let temporary: string;
let pageErrors: string[] = [];
test.beforeAll(() => {
  temporary = mkdtempSync(path.join(tmpdir(), "cosir-terminal-ui-"));
  const root = path.resolve("../..");
  const file = path.join(temporary, "samples.json");
  execFileSync(path.join(root, "apps/backend/.venv/bin/python"), [path.join(root, "apps/backend/tests/fixtures/terminal_output_samples.py"), file], {
    cwd: root, timeout: 30000,
    env: { ...process.env, PYTHONPATH: path.join(root, "apps/backend"), CODING_AGENT_TERMINAL_WORKER: path.join(root, "target/debug/terminal-worker"), CODING_AGENT_DATA_DIR: temporary },
  });
  samples = JSON.parse(readFileSync(file, "utf8"));
});
test.afterAll(() => rmSync(temporary, { recursive: true, force: true }));
test.beforeEach(async ({ request, page }) => {
  pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "命令输出体验" } });
});
test.afterEach(() => expect(pageErrors).toEqual([]));
const command = "cd /Users/example/workspace && printf 'long command'\n" + "printf 'more command arguments' ".repeat(12);
function state(display: TransportToolDisplayData, status: "running" | "completed" | "failed" | "cancelled" = "running", renderedCommand = command) {
  return transportState({
    messages: [userMessage("u", 1, "查看真实命令输出"), assistantMessage("a", 1, [toolCallPart("cmd", "execute_terminal", {
      status, args: { description: "统计代码规模", command: renderedCommand }, display_data: { ...display, command: renderedCommand },
      error: status === "failed" ? "命令超时" : status === "cancelled" ? "已取消" : null,
    })], status === "running" ? null : outcome("completed"))],
    activity: activity(1, status === "running" ? "suspended" : "completed", { startedAt: new Date().toISOString() }),
  });
}
async function openCall(page: Page) {
  const process = page.locator('[data-slot="process-head"]');
  if (await process.getAttribute("aria-expanded") === "false") await process.click();
  await expect(page.locator('[data-slot="activity-head"]')).toBeVisible();
  const group = page.locator('[data-slot="activity-head"]');
  if (await group.count() && await group.getAttribute("aria-expanded") === "false") await group.click();
  const call = page.locator('[data-tool-call-id="cmd"]');
  if (await call.locator('[data-slot="call-row"]').getAttribute("aria-expanded") === "false") await call.locator('[data-slot="call-row"]').click();
  return call;
}
for (const theme of ["light", "dark"]) test(`${theme}: real output, command once, labeled copy and independent reader`, async ({ page, request, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.addInitScript(theme => localStorage.setItem("cosir.theme-preference", theme), theme);
  await page.setViewportSize({ width: 1100, height: 900 });
  await seedState(request, 42, state(samples.ansi.display));
  await page.goto("/tasks/42");
  const call = await openCall(page);
  await expect(call.locator('[data-slot="call-row"]')).not.toContainText("cd /");
  await expect(call.locator('[data-terminal-command]')).toHaveCount(1);
  await expect(call.locator('[data-terminal-command]')).toHaveText(command);
  await call.getByRole("button", { name: "显示完整命令" }).click();
  await expect(call.locator('[data-terminal-command]')).not.toHaveClass(/line-clamp/);
  await call.getByRole("button", { name: "复制命令", exact: true }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(command);
  await call.getByRole("button", { name: "复制输出", exact: true }).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain("中文输出");
  expect(copied).toContain("progress 100%");
  expect(copied).not.toContain("\u001b");
  expect(copied).toContain("120");
  const preview = call.locator('[data-terminal-mode="preview"]');
  expect((await preview.boundingBox())!.height).toBeLessThan(201);
  await expect(preview.locator(".xterm-scrollable-element > .scrollbar.vertical")).toHaveCSS("display", "none");
  await preview.hover();
  const before = await preview.locator(".xterm-rows").innerText();
  await page.mouse.wheel(0, -400);
  await expect.poll(() => preview.locator(".xterm-rows").innerText()).toBe(before);
  await call.getByRole("button", { name: "查看输出" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[data-terminal-mode="reader"]')).toBeVisible();
  await page.screenshot({ path: `${OUTPUT_DIR}/terminal-reader-${theme}.png`, animations: "disabled" });
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(call.getByRole("button", { name: "查看输出" })).toBeFocused();
  await page.screenshot({ path: `${OUTPUT_DIR}/terminal-preview-${theme}.png`, animations: "disabled" });
});

test("reader keeps scroll position through streaming, completion, close and reopen", async ({ page, request }) => {
  await seedState(request, 42, state(samples.ansi.display));
  await page.goto("/tasks/42");
  // 订阅建立后改为运行中，避免测试服务自动推进演示执行者。
  await expect(page.locator('[data-slot="process-head"]')).toBeVisible();
  const live = state(samples.ansi.display);
  live.activity = activity(1, "running", { startedAt: new Date().toISOString() });
  await seedState(request, 42, live);
  const call = page.locator('[data-tool-call-id="cmd"]');
  await call.getByRole("button", { name: "查看输出" }).click();
  const dialog = page.getByRole("dialog");
  const scroll = dialog.locator(".xterm-rows");
  await expect(scroll).toContainText("120");
  await dialog.locator(".xterm-screen").hover();
  await expect.poll(async () => {
    await page.mouse.wheel(0, -800);
    return scroll.innerText();
  }, { timeout: 10000, intervals: [50] }).toContain("中文输出");
  const display = { ...samples.ansi.display, output: String(samples.ansi.display.output) + "new-tail\n" };
  await seedState(request, 42, state(display));
  await expect(dialog.getByRole("button", { name: "有新输出 · 回到最新" })).toBeVisible();
  await expect(scroll).toContainText("中文输出");
  await seedState(request, 42, state(display, "completed"));
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("已结束", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await call.getByRole("button", { name: "查看输出" }).click();
  await expect(scroll).toContainText("中文输出");
  await dialog.getByRole("button", { name: "有新输出 · 回到最新" }).click();
  await expect(scroll).toContainText("new-tail");
  await expect(dialog.getByRole("button", { name: "有新输出 · 回到最新" })).toHaveCount(0);
});

test("retained buffer copy is explicit, raw download includes all real output, copy failures are visible", async ({ page, request }) => {
  await seedState(request, 42, state(samples.long.display));
  await page.goto("/tasks/42");
  const call = await openCall(page);
  await expect(call.getByRole("button", { name: "复制保留内容" })).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await call.getByRole("button", { name: "下载原始输出" }).click();
  const download = await downloadPromise;
  expect(readFileSync((await download.path())!, "utf8")).toBe(samples.long.display.output);
  await page.evaluate(() => Object.defineProperty(navigator.clipboard, "writeText", { value: async () => { throw new Error("clipboard denied"); } }));
  await call.getByRole("button", { name: "复制命令" }).click();
  await expect(call.getByText("复制失败，请重试")).toBeVisible();
});

for (const scenario of ["nonzero", "empty", "timeout", "cancelled"]) test(`${scenario}: preserves real partial output and correct status`, async ({ page, request }) => {
  const sample = samples[scenario === "cancelled" ? "ansi" : scenario];
  const status = scenario === "timeout" ? "failed" : scenario === "cancelled" ? "cancelled" : "completed";
  await seedState(request, 42, state(sample.display, status));
  await page.goto("/tasks/42");
  const call = await openCall(page);
  await expect(call).toHaveAttribute("data-tool-status", status);
  if (scenario === "empty") await expect(call.getByText("无输出", { exact: true })).toBeVisible();
  else await expect(call.getByRole("button", { name: "查看输出" })).toBeVisible();
  await page.setViewportSize({ width: 768, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});


test("a short command clipped by a narrow column still has an expand action", async ({ page, request }) => {
  await page.setViewportSize({ width: 480, height: 900 });
  const shortCommand = "printf '" + "中文参数".repeat(20) + "'";
  expect(shortCommand.length).toBeLessThan(100);
  await seedState(request, 42, state(samples.empty.display, "running", shortCommand));
  await page.goto("/tasks/42");
  const call = await openCall(page);
  await expect(call.getByRole("button", { name: "显示完整命令" })).toBeVisible();
  await call.getByRole("button", { name: "显示完整命令" }).click();
  await expect(call.locator('[data-terminal-command]')).not.toHaveClass(/line-clamp/);
});
