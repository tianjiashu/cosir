import { expect, test, type Locator } from "@playwright/test";
import { completedState, seedState } from "./transport-fixtures";
import { API_ORIGIN } from "./e2e-env";

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
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 100, title: "Markdown 块" } });
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
  // 窄窗口下对话列让给侧栏后放不下原始宽度，图表收进列内是预期行为。
  await page.setViewportSize({ width: 1600, height: 900 });
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

// 目的：能点的东西一眼可辨；潜在缺陷：链接沿用中性主色与正文同色、可打开的路径与普通代码无异、脚注点不动。
for (const scheme of ["light", "dark"] as const) {
  test(`${scheme}：链接与可点路径用链接色，脚注可来回跳转，任务列表与按键样式`, async ({ page, request }) => {
    await page.emulateMedia({ colorScheme: scheme });
    const text = [
      "正文，[文件](README.md#L3)、[网页](https://example.com) 与 `src/app.ts:42`、`plain()`。",
      "脚注引用[^1]。",
      "- [x] 已完成",
      "<kbd>Cmd</kbd>",
      "[^1]: 脚注内容",
    ].join("\n\n");
    await seedState(request, 100, completedState(70, {
      id: "markdown-user",
      parts: [{ type: "text", text: "看链接", status: "completed" }],
    }, [{ type: "text", text, status: "completed" }]));
    await page.reload();
    const answer = page.locator(".aui-md").last();
    const color = (locator: Locator) => locator.evaluate((element) => getComputedStyle(element).color);
    const body = await color(answer.locator("p").first());
    for (const name of ["文件", "网页"]) expect(await color(answer.getByRole("link", { name }))).not.toBe(body);

    const pathCode = answer.locator('[data-slot="workspace-code-link"] code');
    const plainCode = answer.locator("code", { hasText: "plain()" });
    expect(await color(pathCode)).not.toBe(await color(plainCode));
    await expect(pathCode).toHaveCSS("text-decoration-style", "dashed");

    await expect(answer.locator('li:has(> input[type="checkbox"])')).toHaveCSS("list-style-type", "none");
    await expect(answer.locator("kbd")).toHaveCSS("border-bottom-width", "2px");

    const reference = answer.locator("sup a");
    await expect(reference).toHaveAttribute("data-streamdown", "link");
    // 引用与返回两个方向的目标都在同一篇里（清洗不改坏 id），点击只在原文里滚动、不改路由。
    for (const link of [reference, answer.locator("[data-footnote-backref]")]) {
      const id = decodeURIComponent((await link.getAttribute("href"))!.slice(1));
      expect(await answer.evaluate((element, target) => element.querySelector(`#${CSS.escape(target)}`) !== null, id)).toBe(true);
    }
    await reference.click();
    await expect(answer.locator("[data-footnote-backref]")).toBeInViewport();
    await expect(page).toHaveURL(/\/tasks\/100$/);
  });
}
