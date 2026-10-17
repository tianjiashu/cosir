import { expect, test, type Locator } from "@playwright/test";
import type { Editor } from "@tiptap/core";

const placeholder = "描述要做的事，例如：帮我查找登录相关代码…";

async function expectAlignedPlaceholder(input: Locator) {
  await expect(input).toHaveAttribute("aria-placeholder", placeholder);
  const paragraph = input.locator("p").first();
  await expect(paragraph).toHaveClass(/is-editor-empty/);
  await expect(paragraph).toHaveAttribute("data-placeholder", placeholder);
  await expect.poll(() => input.evaluate((element) => {
    const paragraph = element.querySelector("p")!;
    const style = getComputedStyle(element);
    const hint = getComputedStyle(paragraph, "::before");
    const contentTop = element.getBoundingClientRect().top + parseFloat(style.paddingTop);
    return {
      paragraphOffset: paragraph.getBoundingClientRect().top - contentTop,
      hintHeight: hint.height,
      hintFloat: hint.cssFloat,
      hintContent: hint.content,
    };
  })).toEqual({ paragraphOffset: 0, hintHeight: "0px", hintFloat: "left", hintContent: `"${placeholder}"` });
}

async function caretPosition(input: Locator) {
  return input.evaluate((element) => {
    const editor = (element as HTMLElement & { editor: Editor }).editor;
    // 固定首段起点，避免各平台 Home / 全选键的选区语义干扰布局测量。
    const caret = editor.view.coordsAtPos(1);
    const rect = element.getBoundingClientRect();
    // 空段落返回行框，文字返回字形框；比较中线以排除字体上、下留白。
    return { centerY: (caret.top + caret.bottom) / 2 - rect.top, left: caret.left - rect.left };
  });
}

for (const surface of [
  { name: "新对话", path: "/", label: "新对话内容" },
  { name: "任务", path: "/tasks/42", label: "消息输入" },
]) {
  for (const width of [1280, 640]) {
    test(`${surface.name}输入框在 ${width}px 下占位与光标对齐，清空后恢复`, async ({ page, request }, testInfo) => {
      await request.post("http://127.0.0.1:8000/__test__/reset");
      await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "占位对齐回归" } });
      await page.setViewportSize({ width, height: 900 });
      await page.goto(surface.path);
      const input = page.getByRole("textbox", { name: surface.label, exact: true });
      await input.focus();
      await expectAlignedPlaceholder(input);
      const emptyCaret = await caretPosition(input);
      await page.screenshot({ path: testInfo.outputPath("empty-composer.png"), animations: "disabled", caret: "initial" });

      await input.fill("验证输入位置");
      await expect(input.locator(".is-editor-empty")).toHaveCount(0);
      const typedCaret = await caretPosition(input);
      expect(Math.abs(typedCaret.centerY - emptyCaret.centerY)).toBeLessThanOrEqual(1);
      expect(Math.abs(typedCaret.left - emptyCaret.left)).toBeLessThanOrEqual(1);

      await input.fill("");
      await expectAlignedPlaceholder(input);
      // 真实空白内容属于草稿，不应再按 trim() 被当作空文档。
      await input.press("Space");
      await expect(input.locator(".is-editor-empty")).toHaveCount(0);
      await input.press("Backspace");
      await expectAlignedPlaceholder(input);
      await input.press("Shift+Enter");
      await expect(input.locator(".is-editor-empty")).toHaveCount(0);
      await input.press("Backspace");
      await expectAlignedPlaceholder(input);
      await input.blur();
      await expectAlignedPlaceholder(input);
    });
  }
}
