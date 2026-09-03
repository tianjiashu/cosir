import { expect, test } from "@playwright/test";

test("新建对话通过一次 /assistant 请求并在任务页展示响应", async ({ page }) => {
  let assistantRequests = 0;
  let assistantStatus: number | undefined;
  page.on("response", (response) => {
    if (response.url().endsWith("/assistant")) assistantStatus = response.status();
  });
  page.on("pageerror", (error) => console.log(`pageerror: ${error.message}`));

  await page.route("http://127.0.0.1:8000/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (request.method() === "GET" && url.pathname === "/workspaces") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify([{ workspace_id: 7, name: "demo", root_path: "C:/demo", created_at: "2026-01-01", updated_at: "2026-01-01" }]),
      });
      return;
    }
    if (request.method() === "GET" && url.pathname === "/models") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify([{ provider_id: 2, provider_name: "demo", models: [{ model_name: "demo-model", supports_thinking: false, supports_image: false, supports_video: false, supports_reasoning_effort: false }] }]),
      });
      return;
    }
    if (request.method() === "POST" && url.pathname === "/assistant") {
      assistantRequests += 1;
      const body = request.postDataJSON();
      expect(body.workspaceId).toBe(7);
      expect(body.taskId).toBeUndefined();
      expect(body.commands).toHaveLength(1);
      expect(body.commands[0].type).toBe("add-message");
      return route.continue();
    }
    await route.continue();
  });

  await page.goto("/");
  await expect(page.getByText("demo").first()).toBeVisible();
  await page.getByRole("button", { name: "选择工作区" }).click();
  await page.getByRole("option", { name: /demo/ }).click();
  await expect(page.getByRole("button", { name: "demo-model" })).toBeVisible();
  await page.getByLabel("新对话内容").fill("你好");
  await expect(page.getByRole("button", { name: "开始对话" })).toBeEnabled();
  await page.getByRole("button", { name: "开始对话" }).click();

  await expect(page).toHaveURL(/\/tasks\/42$/);
  expect(assistantStatus).toBe(200);
  await expect(page.getByText("world")).toBeVisible();
  expect(assistantRequests).toBe(1);
});
