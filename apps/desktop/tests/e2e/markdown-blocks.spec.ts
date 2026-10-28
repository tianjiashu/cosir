import { expect, test, type Locator } from "@playwright/test";
import { completedState, seedState } from "./transport-fixtures";

/*
 * app/markdown.css 覆盖 Streamdown 的块级外观：表格无外框、按钮悬停才出现。
 * 表格按钮行没有 data-streamdown 标记，只能按结构定位，这里守护 Streamdown
 * 升级后结构变化导致覆盖静默失效。
 */
const markdown = [
  "| 能力 | API |",
  "| --- | --- |",
  "| 出网围栏 | WFP |",
  "| 防提权 | CreateRestrictedToken |",
  "",
  "```python",
  "print(1)",
  "```",
].join("\n");

const style = (locator: Locator, property: "opacity" | "borderTopWidth" | "paddingTop") =>
  locator.evaluate((element, key) => getComputedStyle(element)[key], property);

test.beforeEach(async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 100, title: "Markdown 块" } });
  await seedState(request, 100, completedState(70, {
    id: "markdown-user",
    parts: [{ type: "text", text: "给个表格", status: "completed" }],
  }, [{ type: "text", text: markdown, status: "completed" }]));
  await page.goto("/tasks/100");
});

test("表格去掉外框，按钮悬停才出现且可进入全屏", async ({ page }) => {
  const wrapper = page.locator('.aui-md [data-streamdown="table-wrapper"]').last();
  const actions = wrapper.locator("> div:first-child:not(:last-child)");
  await expect(wrapper.locator('[data-streamdown="table"]')).toBeVisible();
  expect(await style(wrapper, "borderTopWidth")).toBe("0px");
  expect(await style(wrapper, "paddingTop")).toBe("0px");
  expect(await style(wrapper.locator("> div:last-child"), "borderTopWidth")).toBe("1px");
  await page.mouse.move(0, 0);
  await expect.poll(() => style(actions, "opacity")).toBe("0");

  await wrapper.hover();
  await expect.poll(() => style(actions, "opacity")).toBe("1");
  await wrapper.getByRole("button", { name: "View fullscreen" }).click();
  const surface = page.locator('[data-streamdown="table-fullscreen"]');
  await expect(surface).toBeVisible();
  await surface.getByRole("button", { name: "Exit fullscreen" }).click();
  await expect(surface).toBeHidden();
});

test("代码块按钮悬停才出现", async ({ page }) => {
  const block = page.locator('.aui-md [data-streamdown="code-block"]').last();
  const actions = block.locator('[data-streamdown="code-block-actions"]');
  await expect(block).toBeVisible();
  await page.mouse.move(0, 0);
  await expect.poll(() => style(actions, "opacity")).toBe("0");
  await block.hover();
  await expect.poll(() => style(actions, "opacity")).toBe("1");
});
