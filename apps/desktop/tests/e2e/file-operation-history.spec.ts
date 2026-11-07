import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { seedState } from "./transport-fixtures";
import { activity, assistantMessage, outcome, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";

test("一次调用中的同路径操作在汇总与过程详情中都逐条保留", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 49, title: "文件操作" } });
  const keyErrors: string[] = [];
  page.on("console", message => { if (/same key|unique.*key/i.test(message.text())) keyErrors.push(message.text()); });
  await seedState(request, 49, transportState({
    messages: [userMessage("u", 1), assistantMessage("a", 1, [toolCallPart("changes", "apply_patch", {
      status: "completed", display_data: { kind: "file-changes", changes: [
        { path: "a.ts", new_path: "b.ts", status: "moved" },
        { path: "a.ts", new_path: "b.ts", status: "moved" },
      ] },
    })], outcome("completed"))], activity: activity(1, "completed"),
  }));
  await page.goto("/tasks/49");
  const card = page.locator('[data-slot="changes-card"]');
  await expect(card).toContainText("2 次操作");
  await card.getByRole("button").click();
  await expect(card.getByText("移动 → b.ts", { exact: true })).toHaveCount(2);
  await page.locator('[data-slot="activity-head"]').click();
  const call = page.locator('[data-tool-call-id="changes"]');
  await expect(call.locator('[data-slot="call-row"]')).not.toHaveAttribute("aria-expanded");
  expect(keyErrors).toEqual([]);
});
