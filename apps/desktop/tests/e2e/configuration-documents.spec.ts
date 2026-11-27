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
  await page.goto("/configuration/main-agent");
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
  await page.goto("/configuration/main-agent");
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  await editor.fill("# 会失败");
  await page.keyboard.press("ControlOrMeta+s");
  await document.fail("磁盘只读");
  await expect(page.getByText("磁盘只读")).toBeVisible();
  await editor.fill("# 再试一次");
  await expect(page.getByText("磁盘只读")).toHaveCount(0);
});

test("Markdown 配置：不可保存时按钮与快捷键都不发请求", async ({ page }) => {
  const document = await routeDocument(page, "/configuration/main-agent-prompt", mainAgentPrompt);
  await page.goto("/configuration/main-agent");
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  const save = page.getByRole("button", { name: /保存主 Agent 提示词/ });

  // 主 Agent 提示词不允许为空。
  await editor.fill("   ");
  await expect(save).toBeDisabled();
  await page.keyboard.press("ControlOrMeta+s");

  // 超出 Token 预算。
  await editor.fill("超出预算 ".repeat(80));
  await expect(save).toBeDisabled();
  await page.keyboard.press("ControlOrMeta+s");

  await editor.fill("# 合规");
  await expect(save).toBeEnabled();
  await page.keyboard.press("ControlOrMeta+s");
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
  await page.goto("/configuration/instructions");
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
  await page.goto("/workspaces/7/configuration/fileignore");
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

test(".fileignore 与 Markdown 文档一致：标出未保存更改，⌘S 保存", async ({ page }) => {
  const document = await routeDocument(page, "/workspaces/7/configuration/fileignore", {
    content: "node_modules/\n",
    path: ".cosir/.fileignore",
    exists: true,
    rule_count: 1,
    max_rules: 500,
  });
  await page.goto("/workspaces/7/configuration/fileignore");
  const editor = page.getByRole("textbox");
  await expect(editor).toHaveValue("node_modules/\n");
  const unsaved = page.getByText("有未保存更改");
  await expect(unsaved).toHaveCount(0);

  await editor.fill("dist/\n");
  await expect(unsaved).toBeVisible();
  await page.keyboard.press("ControlOrMeta+s");
  await expect.poll(() => document.submitted).toEqual(["dist/\n"]);
  await document.succeed();
  await expect(page.getByText(".fileignore已保存", { exact: true })).toBeVisible();
  await expect(unsaved).toHaveCount(0);
});

test("Markdown 编辑器的格式快捷键优先于同键的全局快捷键", async ({ page }) => {
  await routeDocument(page, "/configuration/main-agent-prompt", mainAgentPrompt);
  await page.goto("/configuration/main-agent");
  const sidebar = page.getByRole("navigation", { name: "工作区会话列表" });
  await expect(sidebar).toBeVisible();
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  await editor.fill("重点");
  await page.keyboard.press("ControlOrMeta+a");

  // ⌘B / Ctrl+B 在编辑器里只加粗，不收起侧栏。
  await page.keyboard.press("ControlOrMeta+b");
  await expect(editor).toHaveText("**重点**");
  await expect(sidebar).toBeVisible();

  // ⌘K / Ctrl+K 在编辑器里只插入链接，不打开命令面板。
  await page.keyboard.press("End");
  await page.keyboard.press("ControlOrMeta+k");
  await expect(editor).toContainText("](");
  await expect(page.getByRole("dialog", { name: "命令面板" })).toHaveCount(0);

  // 离开编辑器后，同一组键回到全局。
  await editor.blur();
  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.getByRole("dialog", { name: "命令面板" })).toBeVisible();
});

// 目的：Markdown 编辑器按着色服务的字形显示标题与强调，深色下照样；潜在缺陷：字形只以 CSS 变量给出却没人接，标题不再加粗。
test("Markdown 编辑器：标题加粗、强调倾斜，深色下保持", async ({ page }) => {
  await routeDocument(page, "/configuration/main-agent-prompt", { ...mainAgentPrompt, content: "# 主 Agent 协议\n*强调*" });
  await page.goto("/configuration/main-agent");
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  const heading = editor.locator(".shiki-token", { hasText: "主 Agent 协议" });
  const emphasis = editor.locator(".shiki-token", { hasText: "强调" });
  await expect(heading).toHaveCSS("font-weight", "700");
  await expect(emphasis).toHaveCSS("font-style", "italic");
  await page.evaluate(() => document.documentElement.classList.add("dark"));
  await expect(heading).toHaveCSS("font-weight", "700");
  await expect(emphasis).toHaveCSS("font-style", "italic");
});

/**
 * 带内容版本的配置文档：GET 返回当前正文与版本；PUT 的 `base_revision` 对不上就 409
 * `configuration_revision_conflict`（与后端同形），对上就写入并换新版本。`externalEdit` 模拟手改 / git / Agent 写文件。
 */
async function routeRevisedDocument<T extends { content: string }>(page: Page, path: string, initial: T) {
  let generation = 0;
  let current = { ...initial, revision: "r0" };
  const submitted: { content: string; base: string | null }[] = [];
  await page.route(`${API_ORIGIN}${path}`, async (route) => {
    if (route.request().method() !== "PUT") return route.fulfill({ json: current });
    const body = route.request().postDataJSON() as { content: string; base_revision: string | null };
    submitted.push({ content: body.content, base: body.base_revision });
    if (body.base_revision !== current.revision) {
      return route.fulfill({
        status: 409,
        json: { detail: { code: "configuration_revision_conflict", message: "配置文件已在别处修改，保存未生效", current_revision: current.revision } },
      });
    }
    current = { ...current, content: body.content, revision: `r${++generation}` };
    await route.fulfill({ json: current });
  });
  return {
    submitted,
    current: () => current.content,
    externalEdit(content: string) {
      current = { ...current, content, revision: `r${++generation}` };
    },
  };
}

// 目的：编辑期间文件被外部改动，保存不再静默覆盖；潜在缺陷：覆盖后基准没跟上这次写入，下一次保存自己和自己冲突。
test("Markdown 配置：外部改动后保存得到冲突，选覆盖以最新版本为基准再存", async ({ page }) => {
  const document = await routeRevisedDocument(page, "/configuration/main-agent-prompt", mainAgentPrompt);
  await page.goto("/configuration/main-agent");
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  await expect(editor).toHaveText("# 主 Agent 协议");
  await editor.fill("# 我的修改");
  document.externalEdit("# Agent 写入的版本");

  await page.getByRole("button", { name: /保存主 Agent 提示词/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("主 Agent 提示词已在别处修改")).toBeVisible();
  expect(document.current()).toBe("# Agent 写入的版本");
  await dialog.getByRole("button", { name: "用我的版本覆盖" }).click();

  await expect(page.getByText("主 Agent 提示词已保存", { exact: true })).toBeVisible();
  expect(document.submitted).toEqual([
    { content: "# 我的修改", base: "r0" },
    { content: "# 我的修改", base: "r1" },
  ]);
  expect(document.current()).toBe("# 我的修改");
  await expect(page.getByText("有未保存更改", { exact: true })).toHaveCount(0);

  // 覆盖后的基准是这次写入的版本：接着改、接着存不再冲突。
  await editor.fill("# 第二次修改");
  await page.keyboard.press("ControlOrMeta+s");
  await expect.poll(() => document.submitted.at(-1)).toEqual({ content: "# 第二次修改", base: "r2" });
  await expect(dialog).toHaveCount(0);
});

test(".fileignore：外部改动后保存选载入，草稿换成外部版本", async ({ page }) => {
  const document = await routeRevisedDocument(page, "/workspaces/7/configuration/fileignore", {
    content: "node_modules/\n", path: ".cosir/.fileignore", exists: true, rule_count: 1, max_rules: 500,
  });
  await page.goto("/workspaces/7/configuration/fileignore");
  const editor = page.getByRole("textbox");
  await expect(editor).toHaveValue("node_modules/\n");
  await editor.fill("dist/\n");
  document.externalEdit("node_modules/\ncoverage/\n");

  await page.getByRole("button", { name: "保存 .fileignore" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "载入外部版本" }).click();

  await expect(editor).toHaveValue("node_modules/\ncoverage/\n");
  await expect(page.getByText("有未保存更改")).toHaveCount(0);
  expect(document.submitted).toEqual([{ content: "dist/\n", base: "r0" }]);
  expect(document.current()).toBe("node_modules/\ncoverage/\n");
});

test("全局 AGENTS.md：冲突时取消，草稿与失败提示都保留", async ({ page }) => {
  const document = await routeRevisedDocument(page, "/configuration/global-instructions", {
    content: "# 本机约束", path: ".cosir/AGENTS.md", token_length: 4, max_tokens: 32768, effective_on: "next_run",
  });
  await page.goto("/configuration/instructions");
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  await editor.fill("# 我的约束");
  document.externalEdit("# git 拉下来的约束");

  await page.getByRole("button", { name: /保存全局 AGENTS\.md/ }).click();
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();

  await expect(page.getByText("配置文件已在别处修改，保存未生效")).toBeVisible();
  await expect(editor).toHaveText("# 我的约束");
  await expect(page.getByText("有未保存更改", { exact: true })).toBeVisible();
  expect(document.current()).toBe("# git 拉下来的约束");
});
