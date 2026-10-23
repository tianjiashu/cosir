import { expect, test, type Locator } from "@playwright/test";
import type { Editor, JSONContent } from "@tiptap/core";

const undo = process.platform === "darwin" ? "Meta+z" : "Control+z";
const redo = process.platform === "darwin" ? "Meta+Shift+z" : "Control+Shift+z";
const file = { type: "attachmentToken", attrs: { kind: "file", tokenId: "token-file" } };
const skill = { type: "skillToken", attrs: { name: "review-code" } };
const reference = { type: "workspaceReference", attrs: { reference: {
  kind: "snippet", path: "src/very-long-component-name-that-needs-truncation.tsx",
  startLine: 12, endLine: 28, content: "const value = 1;", language: "tsx",
} } };

async function setDocument(input: Locator, content: JSONContent[]) {
  await input.evaluate(async (element, content) => {
    // 经真实注册表和编辑器装配视图，不伪造 token DOM；文件接纳入口由 composer-behavior 覆盖。
    const modulePath = "/lib/assistant/attachments/local-attachment-registry.ts";
    const registry = await import(modulePath);
    registry.registerInlineFileMetadata({
      id: "token-file", name: "very-long-attachment-filename-that-needs-truncation.md", contentType: "text/markdown",
    });
    registry.registerLocalAttachment(new File([""], "screen.png"), {
      id: "token-image", name: "screen.png", path: "/tmp/screen.png", kind: "image", contentType: "image/png",
    });
    const editor = (element as HTMLElement & { editor: Editor }).editor;
    editor.commands.setContent({ type: "doc", content });
  }, content);
}

test.beforeEach(async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 100, title: "行内标签" } });
  await page.route("**/tasks/100/assistant/state", route => route.fulfill({ json: {
    runs: [{ runId: 70, status: "completed", endReason: "stop", contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, usage: null, error: null,
      messages: [
        { id: "history-user", role: "user", parts: [{ type: "text", text: "查看 [src/main.ts](cosir-file:src/main.ts)", status: "completed" }] },
        { id: "history-assistant", role: "assistant", parts: [{ type: "text", text: "历史回复", status: "completed" }] },
      ],
    }], current_run_id: 70, approvals: {}, error: null,
  } }));
  await page.goto("/tasks/100");
  await expect(page.getByLabel("消息输入", { exact: true })).toBeEditable();
});

for (const theme of ["light", "dark"]) {
  for (const width of [1280, 640]) {
    test(`${theme} ${width}px：混排不抬高行距，长名称保留后缀，历史引用不变`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      await page.evaluate(theme => document.documentElement.classList.toggle("dark", theme === "dark"), theme);
      const input = page.getByLabel("消息输入", { exact: true });
      const history = page.locator('[data-role="user"] [data-workspace-reference]');
      const historyStyle = () => history.evaluate(element => {
        const style = getComputedStyle(element);
        return [element.className, style.height, style.fontSize, style.borderRadius, style.padding];
      });
      const before = await historyStyle();
      await setDocument(input, [
        { type: "paragraph", content: [{ type: "text", text: "正文行高参考" }] },
        { type: "paragraph", content: [{ type: "text", text: "使用 " }, skill, { type: "text", text: " 检查" }] },
        { type: "paragraph", content: [file] },
        { type: "paragraph", content: [reference] },
        { type: "paragraph", content: [
          { type: "attachmentToken", attrs: { kind: "image", tokenId: "token-image" } },
          { type: "attachmentToken", attrs: { kind: "file", tokenId: "missing" } },
        ] },
      ]);
      await expect(input.locator("[data-inline-token]")).toHaveCount(5);
      const metrics = await input.evaluate(element => {
        const bounds = element.getBoundingClientRect();
        return {
          lines: [...element.querySelectorAll("p")].map(p => p.getBoundingClientRect().height),
          tokens: [...element.querySelectorAll("[data-inline-token]")].map(token => {
            const rect = token.getBoundingClientRect();
            return {
              height: rect.height,
              labelHeight: token.querySelector(".cosir-inline-token-label")!.getBoundingClientRect().height,
              fits: rect.left >= bounds.left && rect.right <= bounds.right,
            };
          }),
          overflow: element.scrollWidth > element.clientWidth,
        };
      });
      expect(metrics.lines[0]).toBe(24);
      expect(metrics.lines[1]).toBe(metrics.lines[0]);
      expect(metrics.tokens.every(token => token.height === 24 && token.fits)).toBe(true);
      expect(metrics.tokens.every(token => token.labelHeight <= 22)).toBe(true);
      expect(metrics.overflow).toBe(false);
      await expect(input.locator(".cosir-inline-token-extension")).toHaveText([".md", ".tsx", ".png"]);
      await expect(input.locator(".cosir-inline-token-detail")).toHaveText(" · L12–28");
      expect(await historyStyle()).toEqual(before);
      await expect(history.locator("[data-inline-token]")).toHaveCount(0);
      expect(before[2]).toBe("12px");
      await input.screenshot({ path: testInfo.outputPath("tokens.png"), animations: "disabled" });
      await input.locator("[data-attachment-token] [data-inline-token]").first().hover();
      await expect(page.getByRole("tooltip")).toContainText("very-long-attachment-filename-that-needs-truncation.md");
    });
  }
}

for (const [name, token, removeLabel] of [
  ["附件", file, "移除附件 very-long-attachment-filename-that-needs-truncation.md"],
  ["失效附件", { type: "attachmentToken", attrs: { kind: "file", tokenId: "missing" } }, "移除附件"],
  ["Skill", skill, "移除 Skill review-code"],
  ["引用", reference, "移除引用 very-long-component-name-that-needs-truncation.tsx · L12–28"],
] as const) {
  test(`${name}：立即删除与后续输入分别撤销，重做和焦点正常`, async ({ page }) => {
    const input = page.getByLabel("消息输入", { exact: true });
    await setDocument(input, [{ type: "paragraph", content: [
      { type: "text", text: "前 " }, token, { type: "text", text: " 后" },
    ] }]);
    await input.getByRole("button", { name: removeLabel, exact: true }).click();
    await expect(input).toBeFocused();
    await expect(input).toHaveText("前  后");
    await page.keyboard.insertText("新");
    await expect(input).toHaveText("前 新 后");
    await input.press(undo);
    await expect(input).toHaveText("前  后");
    await input.press(undo);
    await expect(input.locator("[data-inline-token]")).toHaveCount(1);
    await input.press(redo);
    await expect(input).toHaveText("前  后");
    await input.press(redo);
    await expect(input).toHaveText("前 新 后");
  });
}

test("键盘可聚焦查看和移除，禁用编辑器不能移除", async ({ page, browserName }) => {
  const input = page.getByLabel("消息输入", { exact: true });
  await setDocument(input, [{ type: "paragraph", content: [skill, reference] }]);
  await input.focus();
  // macOS WebKit 的完整控件导航使用 Option+Tab，保留宿主的原生键盘约定。
  await page.keyboard.press(process.platform === "darwin" && browserName === "webkit" ? "Alt+Tab" : "Tab");
  const remove = input.getByRole("button", { name: "移除 Skill review-code" });
  await expect(remove).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(input.locator("[data-skill-token]")).toHaveCount(0);
  await expect(input).toBeFocused();
  await input.evaluate(element => (element as HTMLElement & { editor: Editor }).editor.setEditable(false, false));
  // 绕过父级 disabled 外观，验证事务边界仍拒绝只读编辑器中的删除。
  await input.getByRole("button", { name: /移除引用/ }).evaluate(element => (element as HTMLButtonElement).click());
  await expect(input.locator("[data-workspace-reference]")).toHaveCount(1);
});
