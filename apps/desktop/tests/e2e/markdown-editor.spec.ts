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
  await page.goto("/configuration/main-agent");
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

// 目的：编辑器的键位都来自快捷键登记表；潜在缺陷：CodeMirror 自带键表抢走 Markdown 命令（⌘I 斜体被「扩大选区」吃掉）、
// 查找打开的是 CodeMirror 自己的英文搜索面板。
test("Markdown 命令优先于编辑器功能键，查找用应用的查找条", async ({ page }) => {
  const editor = await openMainAgentPrompt(page, "协议正文");
  await editor.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("ControlOrMeta+i");
  await expect(editor).toContainText("*协议正文*");

  await page.keyboard.press("ControlOrMeta+f");
  const find = page.getByRole("search");
  await expect(find).toBeVisible();
  await expect(page.locator(".cm-search")).toHaveCount(0);
  await find.getByLabel("在源码中查找").fill("正文");
  await expect(find).toContainText("1/1");
  await page.keyboard.press("Escape");
  await expect(find).toBeHidden();
  await expect(editor).toBeFocused();
});

// 目的：补全弹层的键高于光标移动与换行，Esc 收拢多选区；潜在缺陷：↓ 移动了光标、回车直接换行，多光标收不回来。
test("补全弹层用方向键与回车选择，Esc 收拢多个光标", async ({ page }) => {
  const editor = await openMainAgentPrompt(page, "");
  await editor.click();
  await page.keyboard.type("<di");
  const options = page.locator(".cm-tooltip-autocomplete li");
  await expect(options.first()).toBeVisible();
  await page.keyboard.press("ArrowDown");
  await expect(page.locator(".cm-tooltip-autocomplete li[aria-selected]")).toHaveCount(1);
  const chosen = await page.locator(".cm-tooltip-autocomplete li[aria-selected]").innerText();
  // CodeMirror 在弹层刚变化后的 75ms 内忽略确认（interactionDelay，防误触），真人按键不会这么快。
  await page.waitForTimeout(150);
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-tooltip-autocomplete")).toHaveCount(0);
  await expect(editor).toContainText(chosen.trim().split(/\s/)[0]);
  await expect(editor.locator(".cm-line")).toHaveCount(1);

  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type("ab ab");
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("Shift+ArrowRight");
  await page.keyboard.press("Shift+ArrowRight");
  await page.keyboard.press("ControlOrMeta+d");
  await page.keyboard.press("Escape");
  await page.keyboard.type("X");
  await expect(editor).toContainText("X ab");
});

// 目的：可编辑编辑器的查找条带替换，查找下一处有快捷键，编辑后计数跟着变；潜在缺陷：换成应用查找条后丢了替换、
// F3 / ⌘G 失效、全部替换要撤销多次、改了文字计数还是旧的。
test("查找下一处、替换与全部替换（一次撤销复原），编辑后计数刷新", async ({ page }) => {
  const editor = await openMainAgentPrompt(page, "cat dog cat dog cat");
  await editor.click();
  await page.keyboard.press("ControlOrMeta+f");
  const find = page.getByRole("search");
  await find.getByLabel("在源码中查找").fill("cat");
  await expect(find).toContainText("1/3");
  await editor.focus();
  await page.keyboard.press(process.platform === "darwin" ? "Meta+g" : "F3");
  await expect(find).toContainText("2/3");
  await page.keyboard.press(process.platform === "darwin" ? "Meta+Shift+g" : "Shift+F3");
  await expect(find).toContainText("1/3");

  await page.keyboard.press(process.platform === "darwin" ? "Meta+Alt+f" : "Control+h");
  const replacement = find.getByLabel("替换为");
  await expect(replacement).toBeFocused();
  await replacement.fill("cow");
  await find.getByRole("button", { name: "替换", exact: true }).click();
  await expect(editor).toContainText("cow dog cat dog cat");
  await expect(find).toContainText("1/2");
  // 全部替换的键来自登记表（`replaceAll`）：修饰键要精确，多按一个 Shift 只是替换当前处。
  await replacement.focus();
  await page.keyboard.press("ControlOrMeta+Shift+Enter");
  await expect(editor).toContainText("cow dog cow dog cat");
  await editor.focus();
  await page.keyboard.press("ControlOrMeta+z");
  await expect(editor).toContainText("cow dog cat dog cat");
  await replacement.focus();
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect(editor).toContainText("cow dog cow dog cow");
  await expect(find).toContainText("无结果");
  await editor.focus();
  await page.keyboard.press("ControlOrMeta+z");
  await expect(editor).toContainText("cow dog cat dog cat");

  await find.getByLabel("在源码中查找").fill("dog");
  await expect(find).toContainText("/2");
  await editor.focus();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.type(" dog");
  await expect(find).toContainText("/3");
});

// 切到预览时停在源码视口顶部那一行所在的块（与文件面板同一条规则），不是退到最近的标题。
test("切到预览时停在源码视口所在的那一段", async ({ page }) => {
  const filler = (label: string) => Array.from({ length: 80 }, (_, index) => `${label} 第 ${index} 行`).join("\n\n");
  const editor = await openMainAgentPrompt(page, `# 开头\n\n${filler("前")}\n\n# 结尾章节\n\n${filler("后")}`);
  const topText = await editor.evaluate(async (content) => {
    const scroller = content.closest(".cm-scroller")!;
    scroller.scrollTop = scroller.scrollHeight / 2;
    // CodeMirror 只渲染视口附近的行，等它按新位置重绘。
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const top = scroller.getBoundingClientRect().top;
    return [...content.querySelectorAll(".cm-line")].find((line) => line.textContent && line.getBoundingClientRect().bottom > top + 1)!.textContent!;
  });
  await page.getByRole("button", { name: "查看预览" }).click();
  await expect(page.getByText(topText, { exact: true })).toBeInViewport();
  await expect(page.getByRole("heading", { name: "开头" })).not.toBeInViewport();
});

test("配置页编辑器填满内容区，保存栏始终在视口内", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await openMainAgentPrompt(page, Array.from({ length: 200 }, (_, index) => `第 ${index} 行`).join("\n"));
  await expect(page.getByRole("button", { name: /保存主 Agent 提示词/ })).toBeInViewport({ ratio: 1 });
  await expect(page.getByText("估算 Token", { exact: true })).toBeInViewport({ ratio: 1 });
});
