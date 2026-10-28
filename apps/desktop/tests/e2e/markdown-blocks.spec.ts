import { expect, test, type Locator } from "@playwright/test";
import { completedState, seedState } from "./transport-fixtures";

/*
 * app/markdown.css 覆盖 Streamdown 的块级外观：表格无外框、按钮悬停才出现。
 * 表格按钮行没有 data-streamdown 标记，只能按结构定位，这里守护 Streamdown
 * 升级后结构变化导致覆盖静默失效。Mermaid 图表包装层与缩放控件同理。
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
  "",
  "```mermaid",
  "sequenceDiagram",
  "    participant P as 规划(planner)",
  "    participant C as 实现(coder)",
  "    participant R as 评审(reviewer)",
  "    P->>C: ready + 6 段式计划",
  "    C->>R: done + 产出文件",
  "    R-->>P: pass",
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

test("Mermaid 按原始宽度显示，缩放控件悬停才出现", async ({ page }) => {
  const block = page.locator('.aui-md [data-streamdown="mermaid-block"]').last();
  const svg = block.locator('[aria-label="Mermaid chart"] > svg');
  const zoom = block.getByRole("button", { name: "Zoom in" }).locator("..");
  await expect(svg).toBeVisible();
  // 包装层收缩时 SVG 会回落到 300px 默认宽度；撑满后应接近 Mermaid 声明的原始宽度。
  const { width, maxWidth } = await svg.evaluate((element) => ({
    width: element.getBoundingClientRect().width,
    maxWidth: Number.parseFloat(getComputedStyle(element).maxWidth),
  }));
  expect(maxWidth).toBeGreaterThan(300);
  expect(Math.abs(width - maxWidth)).toBeLessThan(2);
  await page.mouse.move(0, 0);
  await expect.poll(() => style(zoom, "opacity")).toBe("0");
  await block.hover();
  await expect.poll(() => style(zoom, "opacity")).toBe("1");
});
