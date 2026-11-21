import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { readState } from "./transport-fixtures";
import { activity, assistantMessage, outcome, toolCallPart, userMessage } from "../unit/transport-fixtures";
import type { TransportMessagePart, TransportState } from "../../lib/assistant/contract";

/**
 * 工具详情的统一高度上限：长列表、长 diff、超长终端行、长委派说明收起时不超过上限，可显示全部再收起；短详情不出现入口。
 * 潜在缺陷：长内容撑满时间线、终端截掉了结尾、补丁没下发时误报、短详情多一个按钮。
 */
const BUDGET = 384;
const rows = Array.from({ length: 200 }, (_, i) => ({ path: `src/module-${i}.ts`, line: i + 1, content: `const needle${i} = ${i};`, is_match: true }));
const longPatch = ["diff --git a/big.ts b/big.ts", "--- a/big.ts", "+++ b/big.ts", "@@ -1,0 +1,600 @@", ...Array.from({ length: 600 }, (_, i) => `+line ${i}`)].join("\n") + "\n";
const longLine = `${"x".repeat(50_000)}END_OF_OUTPUT`;
const parts: TransportMessagePart[] = [
  { type: "text", text: "开始。" },
  toolCallPart("search", "search_content", { status: "completed", display_data: { kind: "content-search-results", pattern: "needle", path: ".", matches: rows, page: { offset: 0, total: 1000 }, match_count: 1000 } }),
  toolCallPart("edit", "write_file", { status: "completed", display_data: { kind: "file-changes", changes: [{ path: "big.ts", status: "modified", patch: longPatch, insertions: 600, deletions: 0 }] } }),
  toolCallPart("huge", "write_file", { status: "completed", display_data: { kind: "file-changes", changes: [{ path: "huge.ts", status: "modified", patch: null, insertions: 90000, deletions: 3 }] } }),
  toolCallPart("term", "execute_terminal", { status: "completed", args: { command: "cat big.json" }, display_data: { kind: "terminal-result", command: "cat big.json", workdir: "/work", output: `${longLine}\n`, end: "exited", exit_code: 0 } }),
  toolCallPart("few", "find_files", { status: "completed", display_data: { kind: "file-list", files: [{ path: "a.ts" }, { path: "b.ts" }], pattern: "*.ts", path: ".", page: { offset: 0, total: 2 }, match_count: 2 } }),
  { type: "text", text: "然后委派。" },
  toolCallPart("delegate", "delegate_task", { status: "completed", presentation: "standalone", args: { message: "任务说明。".repeat(600) }, display_data: { kind: "delegation-result", title: "子任务", child_agent_id: "worker" } }),
  { type: "text", text: "告一段落。" },
];

async function open(page: Page, request: APIRequestContext) {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-mermaid`);
  const base: TransportState = await readState(request, 42);
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId: 42, hold: true, state: {
    ...base, messages: [userMessage("u", 1, "极端详情"), assistantMessage("a", 1, parts, outcome("completed"))], activity: activity(1, "completed"),
  } } });
  await page.setViewportSize({ width: 1100, height: 900 });
  await page.goto("/sessions/42");
  await page.locator('[data-slot="activity-head"]').first().click();
}
const call = (page: Page, id: string) => page.locator(`[data-tool-call-id="${id}"]`);
async function expand(page: Page, id: string) {
  await call(page, id).locator('[data-slot="call-row"]').click();
  return call(page, id).locator('[data-slot="detail-budget"]').first();
}
async function expectBounded(page: Page, id: string) {
  const budget = await expand(page, id);
  const box = budget.locator("[data-detail-collapsed]");
  await expect(box).toBeVisible();
  expect((await box.boundingBox())!.height).toBeLessThanOrEqual(BUDGET + 1);
  const toggle = budget.getByRole("button", { name: "显示全部" });
  await toggle.click();
  expect((await budget.locator("> div").first().boundingBox())!.height).toBeGreaterThan(BUDGET + 50);
  await budget.getByRole("button", { name: "收起" }).click();
  expect((await budget.locator("> div").first().boundingBox())!.height).toBeLessThanOrEqual(BUDGET + 1);
  return budget;
}

test("long list, diff and delegation brief are bounded, expandable and collapsible", async ({ page, request }) => {
  await open(page, request);
  await expectBounded(page, "search");
  await expect(call(page, "search").locator('[data-slot="list-page-range"]')).toHaveText("第 1–200 条 · 共 1000 条");
  await expectBounded(page, "edit");
  await expectBounded(page, "delegate");
  await page.screenshot({ path: test.info().outputPath("bounded-details.png"), animations: "disabled" });
});

test("a 50KB terminal line keeps its ending visible and the card stays bounded", async ({ page, request }) => {
  await open(page, request);
  const budget = await expand(page, "term");
  const box = budget.locator("[data-detail-collapsed]");
  expect((await box.boundingBox())!.height).toBeLessThanOrEqual(BUDGET + 1);
  // 末尾对齐：结尾那段字在可见区域里。
  const visible = await box.evaluate((el) => {
    const range = document.createRange(); const pre = el.querySelector("pre")!;
    const text = pre.textContent!; const node = [...pre.querySelectorAll("span")].at(-1)!.firstChild!;
    range.setStart(node, (node.textContent ?? "").length - 3); range.setEnd(node, (node.textContent ?? "").length);
    const r = range.getBoundingClientRect(), b = el.getBoundingClientRect();
    return text.endsWith("END_OF_OUTPUT") && r.bottom <= b.bottom + 1 && r.top >= b.top - 1;
  });
  expect(visible).toBe(true);
});

test("an omitted patch says so; a short detail has no toggle", async ({ page, request }) => {
  await open(page, request);
  await call(page, "huge").locator('[data-slot="call-row"]').click();
  await expect(call(page, "huge")).toContainText("改动过大");
  const few = await expand(page, "few");
  await expect(few.getByRole("button", { name: "显示全部" })).toHaveCount(0);
  await expect(call(page, "few").locator('[data-slot="list-page-range"]')).toHaveCount(0);
});
