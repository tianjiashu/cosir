import { expect, test, type Page } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";

// Markdown 编辑器：源码与预览二选一。切换不重建源码编辑器（撤销历史还在），切到预览时停在源码所在的标题；
// 配置页的编辑器填满内容区，保存栏不被挤出视口。

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
});

async function openMainAgentPrompt(page: Page, content: string) {
  await page.route(`${API_ORIGIN}/configuration/main-agent-prompt`, (route) => route.fulfill({
    json: { content, path: ".cosir/main_agent_system_prompt.md", token_length: 5, max_tokens: 20000, source: "user_file", effective_on: "next_run" },
  }));
  await page.goto("/settings/main-agent");
  const editor = page.getByRole("textbox", { name: "Markdown 源码编辑器" });
  await expect(editor).toBeVisible();
  return editor;
}

test("切到预览再切回，源码编辑器的撤销历史还在", async ({ page }) => {
  const editor = await openMainAgentPrompt(page, "# 协议");
  await editor.click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.type("\n补充一行");
  await expect(editor).toContainText("补充一行");

  await page.getByRole("button", { name: "查看预览" }).click();
  await expect(page.getByRole("heading", { name: "协议" })).toBeVisible();
  await expect(editor).toBeHidden();

  await page.getByRole("button", { name: "查看源码" }).click();
  await expect(editor).toBeVisible();
  await editor.click();
  await page.keyboard.press("ControlOrMeta+z");
  await expect(editor).not.toContainText("补充一行");
});

test("切到预览时停在源码视口所在的标题", async ({ page }) => {
  const filler = (label: string) => Array.from({ length: 80 }, (_, index) => `${label} 第 ${index} 行`).join("\n\n");
  const editor = await openMainAgentPrompt(page, `# 开头\n\n${filler("前")}\n\n# 结尾章节\n\n${filler("后")}`);
  await editor.evaluate((content) => {
    const scroller = content.closest(".cm-scroller")!;
    scroller.scrollTop = scroller.scrollHeight;
  });
  await page.getByRole("button", { name: "查看预览" }).click();
  await expect(page.getByRole("heading", { name: "结尾章节" })).toBeInViewport();
  await expect(page.getByRole("heading", { name: "开头" })).not.toBeInViewport();
});

test("配置页编辑器填满内容区，保存栏始终在视口内", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await openMainAgentPrompt(page, Array.from({ length: 200 }, (_, index) => `第 ${index} 行`).join("\n"));
  await expect(page.getByRole("button", { name: /保存主 Agent 提示词/ })).toBeInViewport({ ratio: 1 });
  await expect(page.getByText("估算 Token", { exact: true })).toBeInViewport({ ratio: 1 });
});
