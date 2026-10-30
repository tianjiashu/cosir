import { expect, test, type Locator, type Page } from "@playwright/test";
import { completedState, seedState } from "./transport-fixtures";

const api = "http://127.0.0.1:8000";
const undo = process.platform === "darwin" ? "Meta+z" : "Control+z";
const redo = process.platform === "darwin" ? "Meta+Shift+z" : "Control+Shift+z";
const selectAll = process.platform === "darwin" ? "Meta+a" : "Control+a";

async function nativePicker(page: Page, delayed = false) {
  await page.evaluate((delayed) => {
    Object.assign(window, { __TAURI_INTERNALS__: {
      invoke: async (command: string) => {
        if (command === "plugin:dialog|open") return ["/tmp/notes.md"];
        if (command === "resolve_selected_attachment_path") {
          if (!delayed) return "/tmp/notes.md";
          return new Promise<string>((resolve) => Object.assign(window, { resolvePicked: resolve }));
        }
        if (command === "write_frontend_log") return;
        throw new Error(`Unexpected test IPC: ${command}`);
      },
    } });
  }, delayed);
  await page.getByRole("button", { name: "添加图片、文件或文件夹", exact: true }).click();
  await page.getByRole("button", { name: "选择文件或图片", exact: true }).click();
  if (delayed) await page.waitForFunction(() => "resolvePicked" in window);
  else await expect(page.locator("[data-attachment-token]")).toHaveCount(1);
  await page.keyboard.press("Escape");
}

async function transfer(input: Locator, kind: "paste" | "drop", name: string, type: string, text = "") {
  await input.focus();
  await input.evaluate((element, { kind, name, type, text }) => {
    const data = new DataTransfer();
    const bytes = Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9X8AAAAASUVORK5CYII="), c => c.charCodeAt(0));
    data.items.add(new File([bytes], name, { type }));
    if (text) data.setData("text/plain", text);
    if (kind === "paste") element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
    else for (const event of ["dragenter", "dragover", "drop"]) element.dispatchEvent(new DragEvent(event, { dataTransfer: data, bubbles: true, cancelable: true }));
  }, { kind, name, type, text });
}

async function openComposer(page: Page, mode: "new" | "task") {
  await page.goto(mode === "new" ? "/" : "/tasks/100");
  if (mode === "new") {
    await page.getByRole("button", { name: "选择工作区" }).click();
    await page.getByRole("option", { name: /demo/ }).click();
  }
  const input = page.getByLabel(mode === "new" ? "新对话内容" : "消息输入");
  await expect(input).toBeEditable();
  await expect(page.getByRole("combobox", { name: "选择模型" })).toContainText("demo-model");
  return { input, send: page.getByRole("button", { name: mode === "new" ? "开始对话" : "发送", exact: true }) };
}

test.beforeEach(async ({ page, request }) => {
  await request.post(`${api}/__test__/reset`);
  for (const taskId of [100, 101]) await request.post(`${api}/__test__/seed-task`, { data: { taskId, title: `草稿${taskId}` } });
  await page.addInitScript(() => {
    localStorage.clear();
    for (const scope of ["workspace:7", "workspace:8", "task:100", "task:101"]) {
      localStorage.setItem(`cosir:model-config-selection:${scope}`, JSON.stringify({ modelConfigId: 2, reasoningEffort: null }));
    }
  });
});

test("工作区 A→B→A 保留各自草稿，撤销不会带回另一工作区文本", async ({ page }) => {
  await page.route("**/workspaces", async route => {
    const response = await route.fetch();
    const workspaces = await response.json();
    await route.fulfill({ response, json: [...workspaces, { ...workspaces[0], workspace_id: 8, name: "workspace-B", root_path: "/tmp/B" }] });
  });
  await page.route("**/workspaces/8/tasks", route => route.fulfill({ json: [] }));
  const { input } = await openComposer(page, "new");
  await input.fill("草稿 A");
  await page.getByRole("button", { name: "demo", exact: true }).click();
  await page.getByRole("option", { name: /workspace-B/ }).click();
  await expect(input).toHaveText("");
  await input.press(undo);
  await expect(input).toHaveText("");
  await input.fill("草稿 B");
  await page.getByRole("button", { name: "workspace-B", exact: true }).click();
  await page.getByRole("option", { name: /demo/ }).click();
  await expect(input).toHaveText("草稿 A");
  await input.press(undo);
  await expect(input).toHaveText("草稿 A");
});

test("原生选择在路径解析期间切换工作区，迟到文件不会写入任一草稿", async ({ page }) => {
  await page.route("**/workspaces", async route => {
    const response = await route.fetch();
    const workspaces = await response.json();
    await route.fulfill({ response, json: [...workspaces, { ...workspaces[0], workspace_id: 8, name: "workspace-B", root_path: "/tmp/B" }] });
  });
  await page.route("**/workspaces/8/tasks", route => route.fulfill({ json: [] }));
  const { input, send } = await openComposer(page, "new");
  await nativePicker(page, true);
  await page.getByRole("button", { name: "demo", exact: true }).click();
  await page.getByRole("option", { name: /workspace-B/ }).click();
  await page.evaluate(() => (window as unknown as { resolvePicked: (path: string) => void }).resolvePicked("/tmp/notes.md"));
  await expect.poll(() => page.evaluate(() => (window as unknown as { __cosirFrontendLogs?: { event: string }[] }).__cosirFrontendLogs?.some(log => log.event === "composer_attachment_selection_discarded"))).toBe(true);
  await expect(input).toHaveText("");
  await expect(send).toBeDisabled();
  await page.getByRole("button", { name: "workspace-B", exact: true }).click();
  await page.getByRole("option", { name: /demo/ }).click();
  await expect(input).toHaveText("");
  await expect(send).toBeDisabled();
});

for (const mode of ["new", "task"] as const) {
  test(`${mode} 普通文件删除、撤销、重做与发送失败恢复使用同一文档事实`, async ({ page }) => {
    const { input, send } = await openComposer(page, mode);
    await nativePicker(page);
    await expect(send).toBeEnabled();
    await input.getByRole("button", { name: "移除附件 notes.md" }).click();
    await expect(input.locator("[data-attachment-token]")).toHaveCount(0);
    await expect(send).toBeDisabled();
    await input.press(undo);
    await expect(input).toContainText("notes.md");
    await expect(input.locator('[data-inline-token][data-state="error"]')).toHaveCount(0);
    await input.press(redo);
    await expect(send).toBeDisabled();
    await input.press(undo);
    await input.press(selectAll);
    await input.press("Backspace");
    await expect(send).toBeDisabled();
    await input.press(undo);
    await expect(input).toContainText("notes.md");
    let submitted: { message: { attachments?: { path: string }[] } } | undefined;
    await page.route(`${api}/tasks/*/messages`, async route => {
      submitted = route.request().postDataJSON();
      await route.fulfill({ status: 400, json: { detail: { error: { code: "INVALID_MESSAGE", message: "回归测试拒绝发送", retryable: true } } } });
    });
    await send.click();
    await expect(page.getByRole("dialog")).toContainText("回归测试拒绝发送");
    expect(submitted?.message.attachments).toEqual([expect.objectContaining({ path: "/tmp/notes.md" })]);
    await page.getByRole("dialog").getByRole("button", { name: "知道了" }).click();
    await expect(input).toContainText("notes.md");
    await expect(input.locator('[data-inline-token][data-state="error"]')).toHaveCount(0);
  });

  test(`${mode} 图片粘贴和拖放各接纳一次，普通文件与不支持的图片在入口反馈`, async ({ page }) => {
    const { input, send } = await openComposer(page, mode);
    await transfer(input, "paste", "paste.png", "image/png", "混合正文");
    await expect(input).toHaveText("混合正文");
    // 两种输入框共用同一缩略图，图片以文件名标注。
    await expect(page.getByRole("img", { name: "paste.png", exact: true })).toHaveCount(1);
    await transfer(input, "drop", "drop.jpg", "image/jpeg");
    await expect(page.getByRole("img", { name: "drop.jpg", exact: true })).toHaveCount(1);
    await transfer(input, "paste", "unknown.md", "text/plain");
    await expect(page.getByRole("alert")).toContainText("请使用附件按钮选择文件");
    await transfer(input, "drop", "animation.gif", "image/gif");
    await expect(page.getByRole("alert")).toContainText("仅支持 JPEG 和 PNG");
    await expect(input.locator("[data-attachment-token]")).toHaveCount(0);
    await expect(send).toBeEnabled();
  });
}

test("Task 草稿离开后恢复普通文件，原生选择迟到结果不会写入新 Task", async ({ page }) => {
  const { input } = await openComposer(page, "task");
  await input.fill("草稿 A ");
  await nativePicker(page);
  await page.getByText("草稿101", { exact: true }).click();
  await expect(input).toHaveText("");
  await input.fill("草稿 B");
  await page.getByText("草稿100", { exact: true }).click();
  await expect(input).toContainText("草稿 A");
  await expect(input).toContainText("notes.md");
  await expect(input.locator('[data-inline-token][data-state="error"]')).toHaveCount(0);
  await nativePicker(page, true);
  await page.getByText("草稿101", { exact: true }).click();
  await expect(input).toHaveText("草稿 B");
  await page.evaluate(() => (window as unknown as { resolvePicked: (path: string) => void }).resolvePicked("/tmp/notes.md"));
  await expect(input).toHaveText("草稿 B");
  await page.getByText("草稿100", { exact: true }).click();
  await expect(input.locator("[data-attachment-token]")).toHaveCount(1);
});

test("Shift+Enter 换行，组合输入 Enter 不提交，普通 Enter 只发送一次", async ({ page }) => {
  const { input } = await openComposer(page, "task");
  let sends = 0;
  page.on("request", req => { if (req.url() === `${api}/tasks/100/messages` && req.method() === "POST") sends++; });
  await input.fill("第一行");
  await input.press("End");
  await input.press("Shift+Enter");
  await input.pressSequentially("second");
  expect(sends).toBe(0);
  await input.evaluate(element => {
    element.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    element.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 229, isComposing: true, bubbles: true, cancelable: true }));
    element.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true }));
  });
  expect(sends).toBe(0);
  await input.press("Enter");
  await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
  expect(sends).toBe(1);
});

test("Task 保活但已进入配置页时，迟到文件不写入隐藏草稿", async ({ page }) => {
  const { input } = await openComposer(page, "task");
  await input.fill("保留正文");
  await nativePicker(page, true);
  await page.getByRole("button", { name: "系统配置", exact: true }).click();
  await expect.poll(() => input.evaluate(element => Boolean(element.closest("[inert]")))).toBe(true);
  await page.evaluate(() => (window as unknown as { resolvePicked: (path: string) => void }).resolvePicked("/tmp/notes.md"));
  await expect.poll(() => page.evaluate(() => (window as unknown as { __cosirFrontendLogs?: { event: string }[] }).__cosirFrontendLogs?.some(log => log.event === "composer_attachment_selection_discarded"))).toBe(true);
  await page.getByText("草稿100", { exact: true }).click();
  await expect(input).toHaveText("保留正文");
});

test("重载后的历史普通文件可编辑、撤销移除并重跑，路径由后端绑定恢复", async ({ page, request: apiRequest }) => {
  await seedState(apiRequest, 100, completedState(70, { id: "history-user", parts: [
    { type: "text", text: "查看 ", status: "completed" },
    { type: "file", file: "cosir-local-file:historical", name: "history.md", contentType: "text/markdown" },
  ] }, [{ type: "text", text: "历史回复", status: "completed" }]));
  await openComposer(page, "task");
  await page.reload();
  const user = page.locator('[data-role="user"]');
  await user.hover();
  await user.getByRole("button", { name: "编辑并重跑" }).click();
  const input = page.getByLabel("编辑消息");
  await expect(input).toContainText("history.md");
  await expect(input).toBeEditable();
  await expect(input.locator('[data-inline-token][data-state="error"]')).toHaveCount(0);
  await input.getByRole("button", { name: "移除附件 history.md" }).click();
  await input.press(undo);
  await expect(input).toContainText("history.md");
  const editUrl = `${api}/tasks/100/messages/history-user/rerun`;
  const request = page.waitForRequest(req => req.url() === editUrl && req.method() === "POST");
  await page.route(editUrl, async route => {
    await route.fulfill({ status: 400, json: { detail: { error: { code: "EDIT_REJECTED", message: "历史编辑回归拒绝", retryable: true } } } });
  });
  await page.getByRole("button", { name: "重跑", exact: true }).click();
  const body = (await request).postDataJSON();
  expect(body).toMatchObject({ message: { parts: [{ type: "text", text: expect.stringContaining("[[cosir-file:historical]]") }] } });
  expect(body.message.attachments).toBeUndefined();
  await expect(page.getByRole("dialog")).toContainText("历史编辑回归拒绝");
  await page.getByRole("dialog").getByRole("button", { name: "知道了" }).click();
  await expect(input).toContainText("history.md");
  await expect(input.locator('[data-inline-token][data-state="error"]')).toHaveCount(0);
});

test("Task 图片上传成功但消息提交失败后，正文与完整图片附件均恢复", async ({ page }) => {
  const { input, send } = await openComposer(page, "task");
  const assetId = "a".repeat(64);
  const locator = `cosir-attachment://${assetId}`;
  await page.route(`${api}/workspaces/7/attachments`, route => route.fulfill({ json: {
    id: assetId, name: "uploaded.png", contentType: "image/png", byteSize: 68, width: 1, height: 1, locator, status: "ready",
  } }));
  await page.route(`${api}/workspaces/7/attachments/${assetId}*`, route => route.fulfill({ contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9X8AAAAASUVORK5CYII=", "base64") }));
  await transfer(input, "paste", "uploaded.png", "image/png", "图片失败恢复");
  await expect(page.locator('[data-slot="image-attachment-strip"] [data-slot="image-attachment"]')).toHaveCount(1);
  const request = page.waitForRequest(req => req.url() === `${api}/tasks/100/messages` && req.method() === "POST");
  await page.route(`${api}/tasks/100/messages`, route => route.fulfill({ status: 400, json: { detail: { error: { code: "INVALID_MESSAGE", message: "图片消息回归拒绝", retryable: true } } } }));
  await send.click();
  const body = (await request).postDataJSON();
  expect(body.message.parts).toContainEqual({ type: "image", image: locator });
  await expect(page.getByRole("dialog")).toContainText("图片消息回归拒绝");
  await page.getByRole("dialog").getByRole("button", { name: "知道了" }).click();
  await expect(input).toHaveText("图片失败恢复");
  await expect(page.locator('[data-slot="image-attachment-strip"] [data-slot="image-attachment"]')).toHaveCount(1);
  await expect(send).toBeEnabled();
});
