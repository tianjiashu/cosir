import { expect, test } from "@playwright/test";

test("模型配置编辑回显密码态 API Key，保存后保持窗口打开", async ({ page }) => {
  const config = {
    config_id: 2,
    config_name: "Demo",
    base_url: "https://api.example.com/v1",
    api_key: "test-api-key",
    model_name: "demo-model",
    context_window_k: 128,
    api_key_configured: true,
    enabled: true,
    sort_order: 0,
    created_at: "",
    updated_at: "",
    supports_thinking: false,
    supports_image: false,
    supports_video: false,
    supports_reasoning_effort: false,
  };
  let saved = false;
  await page.route("http://127.0.0.1:8000/model-configs", (route) => route.fulfill({ json: [config] }));
  await page.route("http://127.0.0.1:8000/model-configs/2", async (route) => {
    if (route.request().method() === "PUT") {
      saved = true;
      return route.fulfill({ json: config });
    }
    return route.fulfill({ json: config });
  });

  await page.goto("/");
  await page.getByRole("combobox", { name: "选择模型" }).click();
  await expect(page.getByText("128K 上下文", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "模型设置" }).click();
  await page.getByRole("button", { name: "编辑" }).click();

  const apiKeyField = page.locator("label").filter({ hasText: "API Key" });
  const apiKey = apiKeyField.locator('input[type="password"]');
  await expect(apiKey).toHaveValue("test-api-key");
  await apiKeyField.getByRole("button", { name: "显示 API Key" }).click();
  await expect(apiKeyField.locator('input[type="text"]')).toHaveValue("test-api-key");
  await apiKeyField.getByRole("button", { name: "隐藏 API Key" }).click();
  await page.getByRole("button", { name: "保存配置" }).click();

  await expect.poll(() => saved).toBe(true);
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByText("模型配置已保存", { exact: true })).toBeVisible();
});
