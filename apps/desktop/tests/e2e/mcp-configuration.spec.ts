import { expect, test } from "@playwright/test";
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

test("MCP 服务按作用域分节：状态正常不说话，要动手的事留在行下，操作悬停才出现，编辑后整表保存", async ({ page }) => {
  const saved: unknown[] = [];
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers`, async (route) => {
    if (route.request().method() === "PUT") saved.push(route.request().postDataJSON());
    await route.fulfill({ json: listing });
  });
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers/test`, (route) => route.fulfill({
    json: { status: "completed", success: true, tools: [], error: null, failure_kind: null, agent_available: true },
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

  // 点名称打开编辑框，改参数后保存：整表只提交本工作区的服务。
  await page.getByRole("button", { name: "filesystem", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "编辑 MCP 服务" })).toBeVisible();
  await dialog.getByPlaceholder("每行一个参数").fill("filesystem\n/tmp");
  await dialog.getByRole("button", { name: "保存服务" }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => (saved[0] as { servers: { name: string; args: string[] }[] } | undefined)?.servers.map((item) => [item.name, item.args])).toEqual([
    ["filesystem", ["filesystem", "/tmp"]],
    ["github", []],
    ["old-tool", ["old-tool"]],
  ]);
});

// 潜在缺陷：增删行、拨开关这类不触发表单 change 事件的改动后，弹窗里还挂着上一次的「测试成功」。
test("草稿的测试结论只对应测过的那份内容：改了任何一处就不显示，改回原样又显示", async ({ page }) => {
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers`, (route) => route.fulfill({ json: listing }));
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/mcp-servers/test`, (route) => route.fulfill({
    json: { status: "completed", success: true, tools: [], error: null, failure_kind: null, agent_available: true },
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
