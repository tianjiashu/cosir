import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { activity, assistantMessage, outcome, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";
import type { TransportMessagePart } from "../../lib/assistant/contract";

const state = (parts: TransportMessagePart[]) => transportState({ messages: [userMessage("u", 1), assistantMessage("a", 1, parts, outcome("completed"))], activity: activity(1, "completed") });
test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "详情生命周期" } });
});

test("large details build only on first open, freeze hidden input, and catch up on reopen", async ({ page, request }) => {
  const part = toolCallPart("list", "list_directory", { status: "completed", display_data: { kind: "directory-list", entries: Array.from({ length: 100 }, (_, i) => ({ name: `entry-${i}`, type: "file" })) } });
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { taskId: 42, state: state([part]) } });
  await page.goto("/tasks/42");
  await page.locator('[data-slot="activity-head"]').click();
  await page.evaluate(async () => {
    const path = "/components/assistant-ui/tools/call-view-registry.ts";
    const { CALL_VIEWS } = await import(/* @vite-ignore */ path);
    const original = CALL_VIEWS["directory-list"];
    const counter = { builds: 0 };
    Object.assign(window, { detailCounter: counter });
    CALL_VIEWS["directory-list"] = (...args: unknown[]) => {
      const result = original(...args), render = result.detail.render;
      result.detail.render = () => { counter.builds++; return render(); };
      return result;
    };
  });
  const next = { ...part, display_data: { ...part.display_data!, total_entries: 100 } };
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { taskId: 42, state: state([next]) } });
  const row = page.locator('[data-tool-call-id="list"]');
  await expect(row).toContainText("100 个条目");
  const builds = () => page.evaluate(() => (window as unknown as { detailCounter: { builds: number } }).detailCounter.builds);
  expect(await builds()).toBe(0);
  await row.locator('[data-slot="call-row"]').click();
  await expect(row.getByText("entry-99", { exact: true })).toBeVisible();
  const before = await builds();
  expect(before).toBeGreaterThan(0);
  await row.locator('[data-slot="call-row"]').click();
  await expect(row.getByText("entry-99", { exact: true })).toBeHidden();
  const after = { ...next, display_data: { ...next.display_data, entries: [{ name: "latest-entry", type: "file" }], total_entries: 1 } };
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { taskId: 42, state: state([after]) } });
  await expect(row).toContainText("1 个条目");
  expect(await builds()).toBe(before);
  await row.locator('[data-slot="call-row"]').click();
  await expect(row.getByText("latest-entry", { exact: true })).toBeVisible();
  expect(await builds()).toBeGreaterThan(before);
});

for (const source of ["call", "artifact"] as const) test(`${source}: Diff keeps DOM, internal scroll and full expansion across folding`, async ({ page, request }) => {
  const patch = ["diff --git a/a.ts b/a.ts", "--- a/a.ts", "+++ b/a.ts", "@@ -1 +1,80 @@", "-old", ...Array.from({ length: 80 }, (_, i) => `+line-${i}`), ""].join("\n");
  const part = toolCallPart("write", "write_file", { status: "completed", display_data: { kind: "file-changes", changes: [{ path: "a.ts", status: "modified", patch, insertions: 80, deletions: 1 }] } });
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { taskId: 42, state: state([part]) } });
  await page.goto("/tasks/42");
  if (source === "call") await page.locator('[data-slot="activity-head"]').click();
  const owner = page.locator(source === "call" ? '[data-tool-call-id="write"]' : '[data-slot="changes-card"]');
  const trigger = source === "call" ? owner.locator('[data-slot="call-row"]') : owner.getByRole("button", { name: /a.ts/ });
  await trigger.click();
  const scroll = owner.locator(".overflow-auto");
  await expect(scroll.locator(".diff")).toBeVisible();
  await scroll.evaluate(el => { el.scrollTop = 250; Object.assign(window, { savedDiff: el }); });
  await trigger.click(); await trigger.click();
  expect(await scroll.evaluate(el => el === (window as unknown as { savedDiff: Element }).savedDiff && el.scrollTop === 250)).toBe(true);
  await owner.getByRole("button", { name: /展开全部/ }).click();
  await trigger.click(); await trigger.click();
  await expect(owner.getByRole("button", { name: "收起", exact: true })).toBeVisible();
});

test("terminal preview freezes while hidden and catches up on show", async ({ page, request }) => {
  const part = toolCallPart("cmd", "execute_terminal", { status: "running", args: { command: "echo" }, display_data: { kind: "terminal-result", output: "first\n" } });
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { taskId: 42, state: state([part]) } });
  await page.goto("/tasks/42");
  await page.locator('[data-slot="activity-head"]').click();
  await page.locator('[data-tool-call-id="cmd"] [data-slot="call-row"]').click();
  const row = page.locator('[data-tool-call-id="cmd"]');
  const preview = row.locator('[data-terminal-mode="preview"]');
  await expect(preview).toContainText("first");
  await row.locator('[data-slot="call-row"]').click();
  await expect(preview).toBeHidden();
  const next = { ...part, display_data: { ...part.display_data!, output: "first\nlatest\n" } };
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { taskId: 42, state: state([next, { type: "text", text: "同步完成" }]) } });
  await expect(page.getByText("同步完成", { exact: true })).toBeVisible();
  await expect(preview).not.toContainText("latest");
  await row.locator('[data-slot="call-row"]').click();
  await expect(preview).toContainText("latest");
});

test("reader survives automatic process folding, receives output and returns focus to the process heading", async ({ page, request }) => {
  const part = toolCallPart("cmd", "execute_terminal", { status: "running", args: { command: "echo" }, display_data: { kind: "terminal-result", output: "first\n" } });
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { taskId: 42, state: state([part]) } });
  await page.goto("/tasks/42");
  await page.locator('[data-slot="activity-head"]').click();
  await page.locator('[data-tool-call-id="cmd"] [data-slot="call-row"]').click();
  await page.getByRole("button", { name: "查看输出" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.locator(".xterm-rows")).toContainText("first");
  const next = { ...part, status: "completed" as const, display_data: { ...part.display_data!, output: "first\nlast-output\n" } };
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { taskId: 42, state: state([next, { type: "text", text: "最终回复", finalReply: true }]) } });
  await expect(dialog.locator(".xterm-rows")).toContainText("last-output");
  await expect(page.locator('[data-slot="process-head"]')).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator('[data-terminal-mode="preview"]')).toBeHidden();
  await page.keyboard.press("Escape");
  await expect(page.locator('[data-slot="process-head"]')).toBeFocused();
});
