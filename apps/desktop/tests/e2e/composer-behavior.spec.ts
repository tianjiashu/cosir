import { expect, test, type FileChooser, type Locator, type Page } from "@playwright/test";
import { installFakeDesktopHost } from "./fake-desktop-host";
import { completedState, seedState } from "./transport-fixtures";
import { API_ORIGIN as api } from "./e2e-env";

const undo = process.platform === "darwin" ? "Meta+z" : "Control+z";
const redo = process.platform === "darwin" ? "Meta+Shift+z" : "Control+Shift+z";
const selectAll = process.platform === "darwin" ? "Meta+a" : "Control+a";

const pickedNotes = { name: "notes.md", mimeType: "text/markdown", buffer: Buffer.from("# notes") };

/**
 * 经「+」菜单的「文件或图片」打开系统文件选择框（只替代操作系统边界）。选择框打开前菜单已收起。
 * 返回选择框：调用方可以先切走会话，再交付文件，模拟用户迟迟才选好的情形。
 */
async function openNativePicker(page: Page): Promise<FileChooser> {
  await page.getByRole("button", { name: "添加", exact: true }).click();
  const chooser = page.waitForEvent("filechooser");
  await page.getByRole("option", { name: /^文件或图片/ }).click();
  return chooser;
}

async function nativePicker(page: Page) {
  await (await openNativePicker(page)).setFiles(pickedNotes);
  await expect(page.locator('[data-capsule-token][data-capsule-kind="file"]')).toHaveCount(1);
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

/** 一次粘贴多张图片（同一个剪贴板事件），模拟一次选中一批文件。 */
async function pasteImages(input: Locator, names: readonly string[]) {
  await input.focus();
  await input.evaluate((element, names) => {
    const data = new DataTransfer();
    const bytes = Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9X8AAAAASUVORK5CYII="), c => c.charCodeAt(0));
    for (const name of names) data.items.add(new File([bytes], name, { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  }, names);
}

async function openComposer(page: Page, mode: "new" | "task", host: { directory?: string } = {}) {
  await installFakeDesktopHost(page, host);
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
  await page.getByRole("button", { name: "选择工作区，当前 demo" }).click();
  await page.getByRole("option", { name: /workspace-B/ }).click();
  await expect(input).toHaveText("");
  await input.press(undo);
  await expect(input).toHaveText("");
  await input.fill("草稿 B");
  await page.getByRole("button", { name: "选择工作区，当前 workspace-B" }).click();
  await page.getByRole("option", { name: /demo/ }).click();
  await expect(input).toHaveText("草稿 A");
  await input.press(undo);
  await expect(input).toHaveText("草稿 A");
});

test("原生选择框打开期间切换工作区，迟到文件不会写入任一草稿", async ({ page }) => {
  await page.route("**/workspaces", async route => {
    const response = await route.fetch();
    const workspaces = await response.json();
    await route.fulfill({ response, json: [...workspaces, { ...workspaces[0], workspace_id: 8, name: "workspace-B", root_path: "/tmp/B" }] });
  });
  await page.route("**/workspaces/8/tasks", route => route.fulfill({ json: [] }));
  const { input, send } = await openComposer(page, "new");
  const chooser = await openNativePicker(page);
  await page.getByRole("button", { name: "选择工作区，当前 demo" }).click();
  await page.getByRole("option", { name: /workspace-B/ }).click();
  await chooser.setFiles(pickedNotes);
  await expect.poll(() => page.evaluate(() => (window as unknown as { __cosirFrontendLogs?: { event: string }[] }).__cosirFrontendLogs?.some(log => log.event === "composer_attachment_selection_discarded"))).toBe(true);
  await expect(input).toHaveText("");
  await expect(send).toBeDisabled();
  await page.getByRole("button", { name: "选择工作区，当前 workspace-B" }).click();
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
    await expect(input.locator('[data-capsule-token][data-capsule-kind="file"]')).toHaveCount(0);
    await expect(send).toBeDisabled();
    await input.press(undo);
    await expect(input).toContainText("notes.md");
    await input.press(redo);
    await expect(send).toBeDisabled();
    await input.press(undo);
    await input.press(selectAll);
    await input.press("Backspace");
    await expect(send).toBeDisabled();
    await input.press(undo);
    await expect(input).toContainText("notes.md");
    let submitted: { message: { parts: { type: string; text: string }[] } } | undefined;
    await page.route(`${api}/tasks/*/messages`, async route => {
      submitted = route.request().postDataJSON();
      await route.fulfill({ status: 400, json: { detail: { error: { code: "INVALID_MESSAGE", message: "回归测试拒绝发送", retryable: true } } } });
    });
    await send.click();
    await expect(page.getByRole("dialog")).toContainText("回归测试拒绝发送");
    expect(submitted?.message.parts).toEqual([
      { type: "text", text: '<file path="/tmp/notes.md" />' },
    ]);
    await page.getByRole("dialog").getByRole("button", { name: "知道了" }).click();
    await expect(input).toContainText("notes.md");
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
    await expect(page.getByRole("alert")).toContainText("不是本机文件");
    await transfer(input, "drop", "animation.gif", "image/gif");
    await expect(page.getByRole("alert")).toContainText("仅支持 JPEG 和 PNG");
    await expect(input.locator('[data-capsule-token][data-capsule-kind="file"]')).toHaveCount(0);
    await expect(send).toBeEnabled();
  });

  // 目的：超量图片在添加入口拦下；潜在缺陷：选了超量图片，发送时逐张上传完才被后端整条拒绝。
  test(`${mode} 图片超过单条消息上限时只接纳前 20 张并在入口反馈`, async ({ page }) => {
    const { input } = await openComposer(page, mode);
    const thumbnails = page.locator('[data-slot="image-attachment-strip"] [data-slot="image-tile"]');
    await pasteImages(input, Array.from({ length: 18 }, (_, index) => `first-${index}.png`));
    await expect(thumbnails).toHaveCount(18);
    await pasteImages(input, ["a.png", "b.png", "c.png", "d.png"]);
    await expect(thumbnails).toHaveCount(20);
    await expect(page.getByRole("alert")).toContainText("一条消息最多 20 张图片，已跳过 2 张");
  });
}

for (const mode of ["new", "task"] as const) {
  test(`${mode} 「+」菜单：插入 Skill 与常用提示词，管理打开提示词库，文件夹以目录引用插入`, async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem("cosir:favorite-prompts:v1", JSON.stringify([{ id: "p1", title: "代码审查", content: "请审查这次改动" }]));
    });
    const skill = (name: string, enabled: boolean) => ({ name, description: `${name} 的说明`, enabled, scope: "system", overrides: false });
    await page.route(/\/configuration\/skills$/, (route) => route.fulfill({ json: [skill("release-notes", true), skill("disabled-skill", false)] }));
    const { input } = await openComposer(page, mode, { directory: "C:/demo/docs" });
    const addButton = page.getByRole("button", { name: "添加", exact: true });

    // Skill 页只列已启用的，可搜索，选中后插入 Skill 胶囊并收起菜单。
    await addButton.click();
    await page.getByRole("option", { name: /^Skill/ }).click();
    await expect(page.getByRole("option", { name: /disabled-skill/ })).toHaveCount(0);
    await page.getByRole("combobox", { name: "搜索 Skill" }).fill("release");
    await page.keyboard.press("Enter");
    await expect(input.locator('[data-capsule-token][data-capsule-kind="skill"]')).toHaveCount(1);
    await expect(page.getByRole("combobox", { name: "搜索 Skill" })).toHaveCount(0);

    // 第一层可以只用键盘：方向键移到「常用提示词」，回车进入，返回后回到第一层。
    await addButton.click();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await page.getByRole("button", { name: "返回", exact: true }).click();
    await expect(page.getByRole("option", { name: /^文件夹/ })).toBeVisible();
    await page.getByRole("option", { name: /^常用提示词/ }).click();
    await page.getByRole("option", { name: /代码审查/ }).click();
    await expect(input).toContainText("请审查这次改动");

    await addButton.click();
    await page.getByRole("option", { name: /^常用提示词/ }).click();
    await page.getByRole("button", { name: "管理", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "管理常用提示词" })).toBeVisible();
    await page.keyboard.press("Escape");

    await addButton.click();
    await page.getByRole("option", { name: /^文件夹/ }).click();
    await expect(input.locator('[data-capsule-token][data-capsule-kind="file"]')).toHaveCount(1);
    await expect(input).toContainText("docs");
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
  const chooser = await openNativePicker(page);
  await page.getByText("草稿101", { exact: true }).click();
  await expect(input).toHaveText("草稿 B");
  await chooser.setFiles(pickedNotes);
  await expect(input).toHaveText("草稿 B");
  await page.getByText("草稿100", { exact: true }).click();
  await expect(input.locator('[data-capsule-token][data-capsule-kind="file"]')).toHaveCount(1);
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
  const chooser = await openNativePicker(page);
  await page.getByRole("button", { name: "系统配置", exact: true }).click();
  await expect.poll(() => input.evaluate(element => Boolean(element.closest("[inert]")))).toBe(true);
  await chooser.setFiles(pickedNotes);
  await expect.poll(() => page.evaluate(() => (window as unknown as { __cosirFrontendLogs?: { event: string }[] }).__cosirFrontendLogs?.some(log => log.event === "composer_attachment_selection_discarded"))).toBe(true);
  await page.getByText("草稿100", { exact: true }).click();
  await expect(input).toHaveText("保留正文");
});

test("重载后撤回带普通文件的消息：附件按快照里的路径放回输入框，照常发送", async ({ page, request: apiRequest }) => {
  await seedState(apiRequest, 100, completedState(70, { id: "history-user", parts: [
    { type: "text", text: '查看 <file path="/w/history.md" />', status: "completed" },
  ] }, [{ type: "text", text: "历史回复", status: "completed" }]));
  await openComposer(page, "task");
  await page.reload();
  const user = page.locator('[data-role="user"]');
  await user.hover();
  await user.getByRole("button", { name: "撤回" }).click();
  const input = page.getByLabel("消息输入");
  await expect(input).toContainText("history.md");
  await input.getByRole("button", { name: "移除附件 history.md" }).click();
  await input.press(undo);
  await expect(input).toContainText("history.md");
  const sendUrl = `${api}/tasks/100/messages`;
  const request = page.waitForRequest(req => req.url() === sendUrl && req.method() === "POST");
  await page.route(sendUrl, async route => {
    await route.fulfill({ status: 400, json: { detail: { error: { code: "INVALID_MESSAGE", message: "历史附件回归拒绝", retryable: true } } } });
  });
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const body = (await request).postDataJSON();
  expect(body).toMatchObject({ message: { parts: [{ type: "text", text: expect.stringContaining('<file path="/w/history.md" />') }] } });
  expect(body.message.attachments).toBeUndefined();
  await expect(page.getByRole("dialog")).toContainText("历史附件回归拒绝");
  await page.getByRole("dialog").getByRole("button", { name: "知道了" }).click();
  await expect(input).toContainText("history.md");
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
  await expect(page.locator('[data-slot="image-attachment-strip"] [data-slot="image-tile"]')).toHaveCount(1);
  const request = page.waitForRequest(req => req.url() === `${api}/tasks/100/messages` && req.method() === "POST");
  await page.route(`${api}/tasks/100/messages`, route => route.fulfill({ status: 400, json: { detail: { error: { code: "INVALID_MESSAGE", message: "图片消息回归拒绝", retryable: true } } } }));
  await send.click();
  const body = (await request).postDataJSON();
  expect(body.message.parts).toContainEqual({ type: "image", image: locator });
  await expect(page.getByRole("dialog")).toContainText("图片消息回归拒绝");
  await page.getByRole("dialog").getByRole("button", { name: "知道了" }).click();
  await expect(input).toHaveText("图片失败恢复");
  await expect(page.locator('[data-slot="image-attachment-strip"] [data-slot="image-tile"]')).toHaveCount(1);
  await expect(send).toBeEnabled();
});
