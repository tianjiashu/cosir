import { expect, test } from "@playwright/test";
test("dbg", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await request.post("http://127.0.0.1:8000/__test__/seed-delegation");
  page.on("console", (m) => { if (/\[(un)?mount\]/.test(m.text())) console.log("[c]", m.text()); });
  page.on("request", (r) => { if (r.url().includes("/tasks/501/")) console.log("[r]", r.method(), r.url().replace(/^.*\/tasks/, "/tasks")); });
  await page.addInitScript(() => window.localStorage.clear());
  await page.goto("/tasks/42");
  await expect(page.getByText("子 Agent 已开始工作。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "打开子 Agent工作台：审查代码" }).click();
  await page.waitForTimeout(6000);
});
