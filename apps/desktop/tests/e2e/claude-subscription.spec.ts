import { expect, test, type Page } from "@playwright/test";

const root = "http://127.0.0.1:8000/model-configs/claude-subscription";
const loggedOut = { status: "logged_out", expires_at: null, can_refresh: false, account_label: null };
const connected = { status: "connected", expires_at: 4_000_000_000, can_refresh: true, account_label: "person@example.test" };
const models = [
  { id: "claude-a", display_name: "Claude A", context_window_k: 200, supports_thinking: true, supports_reasoning_effort: true, supports_image: false },
  { id: "claude-b", display_name: "Claude B", context_window_k: 1000, supports_thinking: false, supports_reasoning_effort: false, supports_image: true },
];

async function configure(page: Page) {
  let status = loggedOut as { status: string; expires_at: number | null; can_refresh: boolean; account_label: string | null };
  let saved: Record<string, unknown> | null = null;
  const calls: string[] = [];
  await page.route("http://127.0.0.1:8000/model-configs", async (route) => {
    if (route.request().method() === "POST") {
      saved = route.request().postDataJSON() as Record<string, unknown>;
      return route.fulfill({ json: { ...saved, config_id: 10, enabled: true, api_key_configured: false, sort_order: 0, created_at: "", updated_at: "" } });
    }
    return route.fulfill({ json: [] });
  });
  await page.route(`${root}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname.split("/").at(-1);
    calls.push(path ?? "");
    if (path === "status") return route.fulfill({ json: status });
    if (path === "login") return route.fulfill({ json: { login_id: "login-1", authorization_url: "https://claude.com/cai/oauth/authorize?state=test", expires_at: 4_000_000_000, browser_opened: true } });
    if (path === "complete") {
      expect(route.request().postDataJSON()).toEqual({ login_id: "login-1", code: "test-code#test-state" });
      status = connected;
      return route.fulfill({ json: status });
    }
    if (path === "credentials") {
      status = loggedOut;
      return route.fulfill({ json: status });
    }
    if (path === "cancel") return route.fulfill({ json: {} });
    if (path === "models") return route.fulfill({ json: { models } });
    throw new Error(`Unexpected Claude endpoint: ${path}`);
  });
  await page.route("http://127.0.0.1:8000/model-configs/test", async (route) => route.fulfill({ json: { success: true, elapsed_ms: 6 } }));
  await page.goto("/settings/models");
  await page.getByRole("combobox", { name: "连接方式", exact: true }).selectOption("claude_subscription");
  return { calls, saved: () => saved };
}

test("Claude 登录后保存模型选择和目录能力，退出后禁止复用测试结果", async ({ page }) => {
  const state = await configure(page);
  await expect(page.getByText("未登录", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "测试连接", exact: true })).toBeDisabled();
  await expect(page.getByLabel("API Base URL", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("API Key", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "登录 Claude", exact: true }).click();
  const code = page.getByLabel("授权码", { exact: true });
  await expect(code).toHaveAttribute("type", "password");
  await code.fill("test-code#test-state");
  await page.getByRole("button", { name: "完成登录", exact: true }).click();
  await expect(page.getByText("person@example.test", { exact: true })).toBeVisible();
  await expect(code).toHaveCount(0);
  await page.getByLabel("配置名称", { exact: true }).fill("个人 Claude");
  await page.getByRole("combobox", { name: "模型名称", exact: true }).selectOption("claude-b");
  await expect(page.getByLabel("上下文窗口（K）", { exact: true })).toHaveValue("1000");
  await expect(page.getByLabel("支持图片输入", { exact: true })).toBeChecked();
  await expect(page.getByLabel("支持思考", { exact: true })).not.toBeChecked();
  await page.getByRole("button", { name: "测试连接", exact: true }).click();
  await expect(page.getByRole("button", { name: "保存配置", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect.poll(state.saved).toMatchObject({ provider: "claude_subscription", model_name: "claude-b", context_window_k: 1000, api_key: "", supports_image: true, supports_thinking: false });
  await expect(page.getByRole("button", { name: "保存配置", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(page.getByText("未登录", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "测试连接", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "保存配置", exact: true })).toBeDisabled();
  expect(await page.evaluate(() => JSON.stringify(window.localStorage))).not.toContain("test-code");
});

test("Claude 授权可以取消，模型目录失败可重试", async ({ page }) => {
  const state = await configure(page);
  await page.getByRole("button", { name: "登录 Claude", exact: true }).click();
  await page.getByRole("button", { name: "取消登录", exact: true }).click();
  await expect(page.getByLabel("授权码", { exact: true })).toHaveCount(0);
  expect(state.calls).toContain("cancel");
  await page.getByRole("button", { name: "登录 Claude", exact: true }).click();
  await page.getByLabel("授权码", { exact: true }).fill("test-code#test-state");
  let attempts = 0;
  await page.route(`${root}/models`, async (route) => {
    attempts += 1;
    if (attempts === 1) return route.fulfill({ status: 503, json: { detail: "目录暂时不可用" } });
    return route.fulfill({ json: { models } });
  });
  await page.getByRole("button", { name: "完成登录", exact: true }).click();
  await expect(page.getByText("目录暂时不可用", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "重试模型列表", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "模型名称", exact: true }).locator("option")).toContainText(["请选择模型", "Claude A", "Claude B"]);
});
