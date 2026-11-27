import { expect, test, type Page } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
});

function server(name: string, extra: Record<string, unknown> = {}) {
  return {
    name,
    transport: "stdio",
    command: "uvx",
    args: [name],
    env: {},
    url: "",
    headers: {},
    oauth: null,
    enabled: true,
    status: "available",
    scope: "workspace",
    overrides: false,
    variables: [],
    literal_fields: [],
    ...extra,
  };
}

const listing = {
  servers: [
    server("filesystem"),
    server("github", {
      transport: "streamable_http", command: "", args: [], url: "https://api.example.com/mcp", status: "missing_variables",
      variables: [{ name: "GITHUB_TOKEN", source: "unset", has_default: false }],
    }),
    server("old-tool", { enabled: false, status: "disabled" }),
    server("shared", { scope: "system" }),
  ],
};

test("MCP 服务按作用域分节：状态正常不说话，要动手的事留在行下，操作悬停才出现，编辑后只替换这一个服务", async ({ page }) => {
  const saved: { method: string; path: string; body: unknown }[] = [];
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers**`, async (route) => {
    const request = route.request();
    if (request.method() !== "GET") saved.push({ method: request.method(), path: new URL(request.url()).pathname, body: request.postDataJSON() });
    await route.fulfill({ json: listing });
  });
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers/test`, (route) => route.fulfill({
    json: { status: "completed", success: true, tools: [], error: null, failure_kind: null, needs_sign_in: false, agent_available: true },
  }));
  await page.goto("/workspaces/7/configuration/mcp");

  // 两节：本工作区与继承来的系统级（只读，没有开关与操作）。
  await expect(page.getByRole("heading", { name: "本工作区" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "系统级" })).toBeVisible();
  await expect(page.getByText("· 只读")).toBeVisible();
  await expect(page.getByRole("switch", { name: /shared/ })).toHaveCount(0);

  // 「可用」不再出徽标；要填变量的服务把徽标与变量留在行下。
  await expect(page.getByText("Agent 可用")).toHaveCount(0);
  await expect(page.getByText("需要填写变量")).toBeVisible();
  await expect(page.getByText("GITHUB_TOKEN")).toBeVisible();

  // 操作悬停才出现。
  const editFilesystem = page.getByRole("button", { name: "编辑 filesystem" });
  await expect(editFilesystem).toHaveCSS("opacity", "0");
  await page.getByRole("button", { name: "filesystem", exact: true }).hover();
  await expect(editFilesystem).toHaveCSS("opacity", "1");

  // 点名称打开编辑框，改参数后保存：只替换这一个服务，别的服务不随请求提交。
  await page.getByRole("button", { name: "filesystem", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "编辑 MCP 服务" })).toBeVisible();
  await dialog.getByPlaceholder("每行一个参数").fill("filesystem\n/tmp");
  await dialog.getByRole("button", { name: "保存服务" }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => saved[0]).toMatchObject({
    method: "PUT",
    path: "/workspaces/7/configuration/mcp-servers/filesystem",
    body: { name: "filesystem", args: ["filesystem", "/tmp"] },
  });
});

// 潜在缺陷：增删行、拨开关这类不触发表单 change 事件的改动后，弹窗里还挂着上一次的「测试成功」。
test("草稿的测试结论只对应测过的那份内容：改了任何一处就不显示，改回原样又显示", async ({ page }) => {
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers`, (route) => route.fulfill({ json: listing }));
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers/test`, (route) => route.fulfill({
    json: { status: "completed", success: true, tools: [], error: null, failure_kind: null, needs_sign_in: false, agent_available: true },
  }));
  await page.goto("/workspaces/7/configuration/mcp");

  await page.getByRole("button", { name: "filesystem", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "测试连接" }).click();
  const success = dialog.getByText("本次测试成功");
  await expect(success).toBeVisible();

  const enabled = dialog.getByRole("switch", { name: "启用此服务" });
  await enabled.click();
  await expect(success).toHaveCount(0);
  await enabled.click();
  await expect(success).toBeVisible();

  // 空行不进配置；填了名称才算改动，用按钮移除这一行就回到测过的内容。
  await dialog.getByRole("button", { name: "添加变量" }).click();
  await expect(success).toBeVisible();
  await dialog.getByRole("textbox", { name: "环境变量 名称" }).fill("TOKEN");
  await expect(success).toHaveCount(0);
  await dialog.getByRole("button", { name: "移除 环境变量" }).click();
  await expect(success).toBeVisible();
});

test("MCP 编辑框保存期间只读：回执关闭弹窗时不会吞掉等待期间的输入", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  const saved: unknown[] = [];
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers**`, async (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: listing });
    saved.push(route.request().postDataJSON());
    await gate;
    await route.fulfill({ json: listing });
  });
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers/test`, (route) => route.fulfill({
    json: { status: "completed", success: true, tools: [], error: null, failure_kind: null, needs_sign_in: false, agent_available: true },
  }));
  await page.goto("/workspaces/7/configuration/mcp");
  await page.getByRole("button", { name: "filesystem", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByPlaceholder("每行一个参数").fill("filesystem\n/tmp");
  await dialog.getByRole("button", { name: "保存服务" }).click();
  await expect.poll(() => saved.length).toBe(1);
  for (const field of [dialog.getByRole("textbox", { name: "服务名称" }), dialog.getByPlaceholder("每行一个参数"), dialog.getByRole("switch", { name: "启用此服务" })]) {
    await expect(field).toBeDisabled();
  }
  release();
  await expect(dialog).toHaveCount(0);
});

test("填写变量的对话框保存期间只读", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers`, (route) => route.fulfill({ json: listing }));
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers/test`, (route) => route.fulfill({
    json: { status: "completed", success: true, tools: [], error: null, failure_kind: null, needs_sign_in: false, agent_available: true },
  }));
  await page.route(/\/configuration\/environment(\/variables)?$/, async (route) => {
    if (route.request().method() === "GET") return route.fallback();
    await gate;
    await route.fulfill({ json: {} });
  });
  await page.goto("/workspaces/7/configuration/mcp");
  await page.getByRole("button", { name: "填写" }).click();
  const dialog = page.getByRole("dialog");
  const value = dialog.locator('input[type="password"]');
  await value.fill("token-1");
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(value).toBeDisabled();
  release();
  await expect(dialog).toHaveCount(0);
});

test("JSON 保存的空参数与含换行的参数：表单里只读，只改服务名时原样保存", async ({ page }) => {
  const args = ["--label", "", "line1\nline2"];
  const saved: string[][] = [];
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers**`, async (route) => {
    if (route.request().method() === "PUT") {
      expect(new URL(route.request().url()).pathname).toBe("/workspaces/7/configuration/mcp-servers/filesystem");
      const body = route.request().postDataJSON() as { name: string; args: string[] };
      expect(body.name).toBe("renamed");
      saved.push(body.args);
    }
    await route.fulfill({ json: { servers: [server("filesystem", { args, enabled: false, status: "disabled" })] } });
  });
  await page.goto("/workspaces/7/configuration/mcp");
  await page.getByRole("button", { name: "filesystem", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByPlaceholder("每行一个参数")).not.toBeEditable();
  await expect(dialog.getByText("参数里有空值或换行，按行编辑会改变它们，请切到 JSON 编辑。")).toBeVisible();
  await dialog.getByRole("textbox", { name: "服务名称" }).fill("renamed");
  await dialog.getByRole("button", { name: "保存服务" }).click();
  await expect(dialog).toHaveCount(0);
  expect(saved).toEqual([args]);
});

test("stdio 切到 HTTP 后只提交 HTTP 的字段：隐藏的启动命令与参数不随之保存", async ({ page }) => {
  const saved: Record<string, unknown>[] = [];
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers**`, async (route) => {
    if (route.request().method() === "PUT") saved.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ json: { servers: [server("demo", { args: ["--label", "${DEMO_LABEL}"], env: { KEY: "v" }, enabled: false, status: "disabled" })] } });
  });
  await page.goto("/workspaces/7/configuration/mcp");
  await page.getByRole("button", { name: "demo", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("combobox", { name: "传输方式" }).selectOption("streamable_http");
  await dialog.getByLabel("HTTP endpoint").fill("http://127.0.0.1:8080/mcp");
  await dialog.getByRole("button", { name: "保存服务" }).click();
  await expect(dialog).toHaveCount(0);
  expect(saved[0]).toMatchObject({ transport: "streamable_http", url: "http://127.0.0.1:8080/mcp", command: "", args: [], env: {} });
});

test("页面列表过期时启停与删除只作用于目标服务：请求不携带其他服务，外部新增的服务不会被覆盖", async ({ page }) => {
  const writes: { method: string; path: string; body: unknown }[] = [];
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers**`, async (route) => {
    const request = route.request();
    if (request.method() !== "GET") writes.push({ method: request.method(), path: new URL(request.url()).pathname, body: request.postDataJSON() });
    await route.fulfill({ json: listing });
  });
  await page.goto("/workspaces/7/configuration/mcp");
  await page.getByRole("switch", { name: /filesystem/ }).click();
  await expect.poll(() => writes).toEqual([
    { method: "PATCH", path: "/workspaces/7/configuration/mcp-servers/filesystem", body: { enabled: false } },
  ]);

  await page.getByRole("button", { name: "filesystem", exact: true }).hover();
  await page.getByRole("button", { name: "删除 filesystem" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "删除", exact: true }).click();
  await expect.poll(() => writes.at(-1)).toEqual({ method: "DELETE", path: "/workspaces/7/configuration/mcp-servers/filesystem", body: null });
});

/** 授权接口的受控替身：不做真实 OAuth；`window.open` 换成记录器。 */
async function mockAuthorization(page: Page, { authorizeDelayMs = 0 } = {}) {
  const calls = { cancels: [] as string[], waits: 0 };
  const authServer = server("notion", { transport: "streamable_http", command: "", args: [], url: "https://mcp.example/mcp", status: "authorization_required" });
  await page.addInitScript(() => {
    const opened: string[] = [];
    (window as unknown as { __opened: string[] }).__opened = opened;
    window.open = (url?: string | URL) => { opened.push(String(url)); return null; };
  });
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers`, (route) => route.fulfill({ json: { servers: [authServer] } }));
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers/authorize`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, authorizeDelayMs));
    await route.fulfill({ json: { status: "authorization_pending", flow_id: "flow-1", authorization_url: "https://auth.example/authorize" } }).catch(() => undefined);
  });
  await page.route(`${API_ORIGIN}/configuration/mcp-authorizations/**`, async (route) => {
    if (route.request().method() === "DELETE") {
      calls.cancels.push(new URL(route.request().url()).pathname);
      return route.fulfill({ json: {} });
    }
    calls.waits += 1; // 等用户在浏览器里授权：一直不返回
  });
  const opened = () => page.evaluate(() => (window as unknown as { __opened: string[] }).__opened);
  return { calls, opened };
}

async function switchTab(page: Page, label: string) {
  await page.getByText(label, { exact: true }).first().click();
  await expect(page).toHaveURL(label === "MCP 服务" ? /\/configuration\/mcp$/ : /\/configuration\/skills$/);
}

test("登录授权进行中离开配置页再回来：进度与取消入口还在，取消会结束后端授权", async ({ page }) => {
  const { calls, opened } = await mockAuthorization(page);
  await page.goto("/workspaces/7/configuration/mcp");
  await page.getByRole("button", { name: "登录授权" }).click();
  await expect(page.getByText("已在浏览器打开 auth.example，完成登录后回到这里")).toBeVisible();

  await switchTab(page, "Skills");
  await switchTab(page, "MCP 服务");
  await expect(page.getByText("已在浏览器打开 auth.example，完成登录后回到这里")).toBeVisible();
  expect(calls.cancels).toEqual([]);
  await page.getByRole("button", { name: "取消" }).click();
  await expect.poll(() => calls.cancels).toEqual(["/configuration/mcp-authorizations/flow-1"]);
  expect(await opened()).toEqual(["https://auth.example/authorize"]);
});

test("授权地址还没返回就离开配置页：地址到了照常打开浏览器，回来能看到进度", async ({ page }) => {
  const { calls, opened } = await mockAuthorization(page, { authorizeDelayMs: 1000 });
  await page.goto("/workspaces/7/configuration/mcp");
  await page.getByRole("button", { name: "登录授权" }).click();
  await expect(page.getByText("正在打开登录授权页…")).toBeVisible();
  await switchTab(page, "Skills");
  await expect.poll(opened).toEqual(["https://auth.example/authorize"]);
  await expect.poll(() => calls.waits).toBe(1);

  await switchTab(page, "MCP 服务");
  await expect(page.getByText("已在浏览器打开 auth.example，完成登录后回到这里")).toBeVisible();
  expect(calls.cancels).toEqual([]);
});
