import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { readState } from "./transport-fixtures";
import { activity, assistantMessage, outcome, toolCallPart, userMessage } from "../unit/transport-fixtures";
import type { TransportMessagePart, TransportState } from "../../lib/assistant/contract";

/**
 * 调用行规则：问题信号单独一格、长标题挤不掉；补丁部分写入的改动算进行与汇总；子 Agent 运行中由行说出、完成后安静。
 * 潜在缺陷：窄屏下失败原因被截掉、「它其实改了文件」看不到、并行子 Agent 分不清谁还在跑。
 */
const longIntent = "把配置模块里所有和调度节拍有关的常量统一改成从配置文件读取并补上范围校验";
const parts: TransportMessagePart[] = [
  { type: "text", text: "开始改。" },
  toolCallPart("edit", "edit_file", { status: "failed", intent: longIntent, args: { path: "src/scheduler/config/tick-settings.ts" }, error: "原文未匹配", display_data: { status_hint: "原文未匹配" } }),
  toolCallPart("patch", "apply_patch", {
    status: "failed", intent: "重构路由", error: "部分已写入",
    display_data: { kind: "file-changes", status_hint: "部分已写入", changes: [{ path: "src/router.ts", status: "modified", patch: null, insertions: 12, deletions: 3 }] },
  }),
  toolCallPart("cmd", "execute_terminal", { status: "completed", args: { command: "npm test --watch=false\necho done" }, display_data: { kind: "terminal-result", command: "npm test", workdir: "/w", output: "ok\n", end: "exited", exit_code: 0 } }),
  { type: "text", text: "告一段落。" },
];

async function open(page: Page, request: APIRequestContext, width: number) {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-mermaid`);
  const base: TransportState = await readState(request, 42);
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId: 42, hold: true, state: {
    ...base, messages: [userMessage("u", 1, "改配置"), assistantMessage("a", 1, parts, outcome("failed"))], activity: activity(1, "failed"),
  } } });
  await page.setViewportSize({ width, height: 800 });
  await page.goto("/sessions/42");
  await page.locator('[data-slot="activity-head"]').first().click();
}
const call = (page: Page, id: string) => page.locator(`[data-tool-call-id="${id}"] [data-slot="call-row"]`);

for (const width of [1100, 360]) test(`at ${width}px the failure reason survives a long title`, async ({ page, request }) => {
  await open(page, request, width);
  const row = call(page, "edit");
  const problem = row.locator('[data-slot="call-problem"]');
  await expect(problem).toHaveText("原文未匹配");
  const [p, r] = [(await problem.boundingBox())!, (await row.boundingBox())!];
  expect(p.x + p.width).toBeLessThanOrEqual(r.x + r.width + 1);
  expect(await problem.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  await page.screenshot({ path: test.info().outputPath(`rows-${width}.png`), animations: "disabled" });
});

test("a partly written patch shows its changes on the row and in the segment total", async ({ page, request }) => {
  await open(page, request, 1100);
  const row = call(page, "patch");
  await expect(row.locator('[data-slot="call-problem"]')).toHaveText("部分已写入");
  await expect(row.locator('[data-slot="call-meta"]')).toContainText("+12");
  await expect(page.locator('[data-slot="activity-head"]').first()).toContainText("+12");
  // 没写意图的命令：目标露首行，分得清是哪条。
  await expect(call(page, "cmd").locator('[data-slot="call-target"]')).toHaveText("npm test --watch=false");
});

test("a delegated agent says it is running, then goes quiet when done", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-delegation`);
  const child = await readState(request, 501);
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId: 501, hold: true, state: { ...child, activity: activity(1, "running") } } });
  await page.goto("/sessions/42");
  const row = call(page, "delegate-ref-500");
  await expect(row).toContainText("运行中");
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId: 501, hold: true, state: { ...child, activity: activity(1, "completed") } } });
  await expect(row).not.toContainText("运行中");
  await expect(row).not.toContainText("已完成");
});
