import { expect, test, type Locator } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { seedState, completedState } from "./transport-fixtures";

const LONG_TEXT = Array.from({ length: 40 }, (_, line) => `第 ${line + 1} 条规则：先查清全貌再动手。`).join("\n\n");

async function seedConversation(request: Parameters<typeof seedState>[0], taskId: number, userText: string) {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId, title: "长消息" } });
  await seedState(request, taskId, completedState(1, { id: "u", parts: [{ type: "text", text: userText }] }, [{ type: "text", text: "收到。", finalReply: true }]));
}

/** 两端渐隐长度由滚动驱动动画写进注册过的自定义属性，读计算值即是此刻真实生效的渐隐。 */
const fadeEdges = (editor: Locator) => editor.evaluate((element) => {
  const style = getComputedStyle(element);
  return [style.getPropertyValue("--scroll-fade-mask-start").trim(), style.getPropertyValue("--scroll-fade-mask-end").trim()];
});

// 目的：长用户消息收起后不霸屏、能看全；潜在缺陷：全文铺开把回复推远，或硬切看起来像内容丢失。
test("长用户消息默认收起、底部渐隐，可以展开与收起；短消息不受影响", async ({ page, request }) => {
  await seedConversation(request, 61, LONG_TEXT);
  await page.goto("/tasks/61");
  const bubble = page.locator('[data-role="user"]');
  const content = bubble.locator("[data-user-message-collapsed]");
  const toggle = bubble.getByRole("button", { name: "显示全部" });

  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect((await content.boundingBox())!.height).toBeLessThanOrEqual(240);
  expect(await content.evaluate((element) => getComputedStyle(element).maskImage)).toContain("gradient");

  await toggle.click();
  const collapse = bubble.getByRole("button", { name: "收起" });
  await expect(collapse).toHaveAttribute("aria-expanded", "true");
  await expect(content).toHaveCount(0);
  await expect(bubble.getByText("第 40 条规则：先查清全貌再动手。")).toBeVisible();

  await collapse.click();
  await expect(bubble.getByRole("button", { name: "显示全部" })).toBeVisible();

  await seedConversation(request, 62, "一句话的问题");
  await page.goto("/tasks/62");
  await expect(page.locator('[data-role="user"]').getByText("一句话的问题")).toBeVisible();
  await expect(page.locator('[data-role="user"]').getByRole("button", { name: /显示全部|收起/ })).toHaveCount(0);
});

// 目的：输入框滚动时两端渐隐提示还有内容；潜在缺陷：首屏或正在编辑的末行被淡化，或内容没溢出时也淡。
test("输入框只在还有内容的一侧渐隐", async ({ page, request }) => {
  await seedConversation(request, 63, "开始");
  await page.goto("/tasks/63");
  const editor = page.getByLabel("消息输入");
  await editor.click();
  await page.keyboard.insertText("一行");
  await expect.poll(() => fadeEdges(editor)).toEqual(["0px", "0px"]);

  for (let line = 0; line < 20; line += 1) {
    await page.keyboard.press("Shift+Enter");
    await page.keyboard.insertText(`第 ${line} 行`);
  }
  // 光标在末尾、已滚到底：只有顶端渐隐。
  await expect.poll(() => fadeEdges(editor)).toEqual(["24px", "0px"]);

  await editor.evaluate((element) => { element.scrollTop = 0; });
  await expect.poll(() => fadeEdges(editor)).toEqual(["0px", "24px"]);
});
