import { expect, test, type Page, type Route } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";

// 单文本配置文档（Markdown 指令 / 提示词、.fileignore）共用的编辑契约：草稿跟随服务端、
// 保存期间的新编辑保留、未保存守卫、保存失败提示在编辑后清除，以及不可保存时按钮与快捷键一致。

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
});

/** 拦截一个配置文档：GET 返回当前文档，PUT 记下正文并挂起，直到测试放行。 */
async function routeDocument<T extends { content: string }>(page: Page, path: string, initial: T) {
  let current = initial;
  const submitted: string[] = [];
  let pending: { route: Route; content: string } | null = null;
  await page.route(`${API_ORIGIN}${path}`, async (route) => {
    if (route.request().method() !== "PUT") return route.fulfill({ json: current });
    const content = (route.request().postDataJSON() as { content: string }).content;
    submitted.push(content);
    pending = { route, content };
  });
  return {
    submitted,
    async succeed() {
      await expect.poll(() => pending).not.toBeNull();
      const { route, content } = pending!;
      pending = null;
      current = { ...current, content };
      await route.fulfill({ json: current });
    },
    async fail(message: string) {
      await expect.poll(() => pending).not.toBeNull();
      const { route } = pending!;
      pending = null;
      await route.fulfill({ status: 400, json: { detail: message } });
    },
  };
}

const mainAgentPrompt = {
  content: "# 主 Agent 协议",
  path: ".cosir/main_agent_system_prompt.md",
  token_length: 5,
  max_tokens: 50,
  source: "user_file",
  effective_on: "next_run",
};

test("Markdown 配置：保存期间的新编辑保留，离开需要确认", async ({ page }) => {
  const document = await routeDocument(page, "/configuration/main-agent-prompt", mainAgentPrompt);
  await page.goto("/settings/main-agent");
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  await expect(editor).toHaveText("# 主 Agent 协议");
  await expect(page.getByText("有未保存更改", { exact: true })).toHaveCount(0);

  await editor.fill("# 提交版本");
  await expect(page.getByText("有未保存更改", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /保存主 Agent 提示词/ }).click();
  await expect.poll(() => document.submitted).toEqual(["# 提交版本"]);
  await editor.fill("# 后来修改");
  await document.succeed();
  await expect(page.getByText("主 Agent 提示词已保存", { exact: true })).toBeVisible();
  await expect(editor).toHaveText("# 后来修改");
  await expect(page.getByText("有未保存更改", { exact: true })).toBeVisible();

  await page.getByRole("navigation", { name: "系统配置分类" }).getByRole("link", { name: "全局 AGENTS.md" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  await expect(editor).toHaveText("# 后来修改");
});

test("Markdown 配置：保存失败提示在编辑后清除", async ({ page }) => {
  const document = await routeDocument(page, "/configuration/main-agent-prompt", mainAgentPrompt);
  await page.goto("/settings/main-agent");
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  await editor.fill("# 会失败");
  await page.keyboard.press("Control+s");
  await document.fail("磁盘只读");
  await expect(page.getByText("磁盘只读")).toBeVisible();
  await editor.fill("# 再试一次");
  await expect(page.getByText("磁盘只读")).toHaveCount(0);
});

test("Markdown 配置：不可保存时按钮与快捷键都不发请求", async ({ page }) => {
  const document = await routeDocument(page, "/configuration/main-agent-prompt", mainAgentPrompt);
  await page.goto("/settings/main-agent");
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  const save = page.getByRole("button", { name: /保存主 Agent 提示词/ });

  // 主 Agent 提示词不允许为空。
  await editor.fill("   ");
  await expect(save).toBeDisabled();
  await page.keyboard.press("Control+s");

  // 超出 Token 预算。
  await editor.fill("超出预算 ".repeat(80));
  await expect(save).toBeDisabled();
  await page.keyboard.press("Control+s");

  await editor.fill("# 合规");
  await expect(save).toBeEnabled();
  await page.keyboard.press("Control+s");
  await expect.poll(() => document.submitted).toEqual(["# 合规"]);
});

test("全局 AGENTS.md 允许保存空内容", async ({ page }) => {
  const document = await routeDocument(page, "/configuration/global-instructions", {
    content: "# 本机约束",
    path: ".cosir/AGENTS.md",
    token_length: 4,
    max_tokens: 32768,
    effective_on: "next_run",
  });
  await page.goto("/settings/instructions");
  await page.getByRole("textbox", { name: "Markdown 源码编辑器" }).fill("");
  await page.getByRole("button", { name: /保存全局 AGENTS\.md/ }).click();
  await expect.poll(() => document.submitted).toEqual([""]);
  await document.succeed();
  await expect(page.getByText("全局 AGENTS.md已保存", { exact: true })).toBeVisible();
});

test(".fileignore：保存期间的新编辑保留，失败提示在编辑后清除，离开需要确认", async ({ page }) => {
  const document = await routeDocument(page, "/workspaces/7/configuration/fileignore", {
    content: "node_modules/\n",
    path: ".cosir/.fileignore",
    exists: true,
    rule_count: 1,
    max_rules: 500,
  });
  await page.goto("/workspaces/7/settings/fileignore");
  const editor = page.getByRole("textbox");
  await expect(editor).toHaveValue("node_modules/\n");
  const save = page.getByRole("button", { name: "保存 .fileignore" });

  await editor.fill("dist/\n");
  await save.click();
  await expect.poll(() => document.submitted).toEqual(["dist/\n"]);
  await editor.fill("dist/\nbuild/\n");
  await document.succeed();
  await expect(page.getByText(".fileignore已保存", { exact: true })).toBeVisible();
  await expect(editor).toHaveValue("dist/\nbuild/\n");

  await save.click();
  await document.fail("规则过多");
  await expect(page.getByText("规则过多")).toBeVisible();
  await editor.fill("dist/\n");
  await expect(page.getByText("规则过多")).toHaveCount(0);

  await editor.fill("dist/\ncoverage/\n");
  await page.getByRole("navigation", { name: "工作区配置分类" }).getByRole("link", { name: "AGENTS.md" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  await expect(editor).toHaveValue("dist/\ncoverage/\n");
});
