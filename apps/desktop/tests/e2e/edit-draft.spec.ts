import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { API_ORIGIN as origin } from "./e2e-env";
import { completedState, seedState } from "./transport-fixtures";

const TASK = 42;
const OTHER = 43;
const rerun = `${origin}/tasks/${TASK}/messages/user-1/rerun`;
const rejected = { status: 400, json: { detail: { error: { code: "EDIT_REJECTED", message: "测试拒绝重跑", retryable: true } } } };

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK, title: "甲任务" } });
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: OTHER, title: "乙任务" } });
});

const seed = (request: APIRequestContext, parts: Parameters<typeof completedState>[1]["parts"] = [{ type: "text", text: "原始问题", status: "completed" }]) =>
  seedState(request, TASK, { ...completedState(1, { id: "user-1", parts }, [{ type: "text", text: "回答", status: "completed", finalReply: true }]), title: "甲任务" });

async function startEdit(page: Page, typed = "，加上写了很久的改动") {
  await page.goto(`/tasks/${TASK}`);
  const user = page.locator('[data-role="user"]').last();
  await user.hover();
  await user.getByRole("button", { name: "编辑并重跑" }).click();
  const editor = page.getByLabel("编辑消息");
  await expect(editor).toBeEditable();
  await editor.click();
  await page.keyboard.press("End");
  await page.keyboard.type(typed);
  await expect(editor).toContainText(typed);
  return editor;
}

async function goTo(page: Page, title: string, url: RegExp) {
  await page.getByRole("treeitem").filter({ hasText: title }).click();
  await expect(page).toHaveURL(url);
}

test("编辑中切走 Task 再回来，改动还在，并能继续重跑", async ({ page, request }) => {
  await seed(request);
  await startEdit(page);
  await goTo(page, "乙任务", /\/tasks\/43$/);
  await goTo(page, "甲任务", /\/tasks\/42$/);
  await expect(page.getByLabel("编辑消息")).toContainText("原始问题，加上写了很久的改动");

  const sent = page.waitForRequest(req => req.url() === rerun && req.method() === "POST");
  await page.route(rerun, route => route.fulfill({ status: 200, json: {} }));
  await page.getByRole("button", { name: "重跑", exact: true }).click();
  expect((await sent).postDataJSON().message.parts[0].text).toBe("原始问题，加上写了很久的改动");
});

test("取消编辑后切走再回来，不恢复也不回到编辑态", async ({ page, request }) => {
  await seed(request);
  await startEdit(page);
  await page.getByRole("button", { name: "取消编辑" }).click();
  await expect(page.getByLabel("编辑消息")).toHaveCount(0);
  await goTo(page, "乙任务", /\/tasks\/43$/);
  await goTo(page, "甲任务", /\/tasks\/42$/);
  await expect(page.getByText("原始问题", { exact: true })).toBeVisible();
  await expect(page.getByLabel("编辑消息")).toHaveCount(0);
});

test("重跑成功后不回到编辑态", async ({ page, request }) => {
  await seed(request);
  await startEdit(page);
  await page.route(rerun, route => route.fulfill({ status: 200, json: {} }));
  await page.getByRole("button", { name: "重跑", exact: true }).click();
  await expect(page.getByLabel("编辑消息")).toHaveCount(0);
  await goTo(page, "乙任务", /\/tasks\/43$/);
  await goTo(page, "甲任务", /\/tasks\/42$/);
  await expect(page.getByText("原始问题", { exact: true })).toBeVisible();
  await expect(page.getByLabel("编辑消息")).toHaveCount(0);
});

test("重跑被拒后回到编辑态，改动还在", async ({ page, request }) => {
  await seed(request);
  await startEdit(page);
  await page.route(rerun, route => route.fulfill(rejected));
  await page.getByRole("button", { name: "重跑", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("测试拒绝重跑");
  await page.getByRole("dialog").getByRole("button", { name: "知道了" }).click();
  await expect(page.getByLabel("编辑消息")).toContainText("写了很久的改动");
  await expect(page.getByRole("button", { name: "重跑", exact: true })).toBeEnabled();
});

test("重跑在途时切走，被拒后不打断当前页，回来时回到编辑态", async ({ page, request }) => {
  await seed(request);
  await startEdit(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route(rerun, async route => { await gate; await route.fulfill(rejected); });
  const sent = page.waitForRequest(req => req.url() === rerun && req.method() === "POST");
  await page.getByRole("button", { name: "重跑", exact: true }).click();
  await sent;
  await goTo(page, "乙任务", /\/tasks\/43$/);
  release();
  await expect(page.getByText("发送失败，内容已放回「甲任务」的输入框")).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await goTo(page, "甲任务", /\/tasks\/42$/);
  await expect(page.getByLabel("编辑消息")).toContainText("写了很久的改动");
});

test("带普通文件的编辑切走再回来，文件胶囊与改动都在", async ({ page, request }) => {
  await seed(request, [
    { type: "text", text: "查看 ", status: "completed" },
    { type: "file", file: "cosir-local-file:historical", name: "history.md", contentType: "text/markdown" },
  ]);
  const editor = await startEdit(page, "，再看一眼");
  await expect(editor).toContainText("history.md");
  await goTo(page, "乙任务", /\/tasks\/43$/);
  await goTo(page, "甲任务", /\/tasks\/42$/);
  const restored = page.getByLabel("编辑消息");
  await expect(restored).toContainText("history.md");
  await expect(restored).toContainText("再看一眼");
  await expect(restored.locator('[data-inline-token][data-state="error"]')).toHaveCount(0);
});

test("带图片的编辑切走再回来，图片只有一份", async ({ page, request }) => {
  const image = `cosir-attachment://${"a".repeat(64)}`;
  await page.route(`${origin}/workspaces/*/attachments/*`, route => route.fulfill({ contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", "base64") }));
  await seed(request, [{ type: "text", text: "看图", status: "completed" }, { type: "image", image }]);
  await startEdit(page, "，补一句");
  const tiles = page.locator('[data-slot="image-attachment-strip"] [data-slot="image-tile"]');
  await expect(tiles).toHaveCount(1);
  await goTo(page, "乙任务", /\/tasks\/43$/);
  await goTo(page, "甲任务", /\/tasks\/42$/);
  await expect(page.getByLabel("编辑消息")).toContainText("补一句");
  await expect(tiles).toHaveCount(1);
});

test("编辑行被虚拟列表卸载再挂载时，改动还在，不被草稿覆盖", async ({ page, request }) => {
  const latest = completedState(1, { id: "user-1", parts: [{ type: "text", text: "原始问题", status: "completed" }] }, [{ type: "text", text: "回答", status: "completed", finalReply: true }]);
  const history = Array.from({ length: 60 }, (_, index) => [
    { id: `old-user-${index}`, role: "user" as const, runId: 1, source: null, outcome: null, parts: [{ type: "text" as const, text: `旧问题 ${index}`, status: "completed" as const }] },
    { id: `old-reply-${index}`, role: "assistant" as const, runId: 1, source: null, outcome: null, parts: [{ type: "text" as const, text: `旧回答 ${index}\n\n${"正文".repeat(200)}`, status: "completed" as const }] },
  ]).flat();
  await seedState(request, TASK, { ...latest, messages: [...history, ...latest.messages] });
  const editor = await startEdit(page);
  const viewport = page.locator(".aui-thread-root > div").filter({ has: page.locator('[data-role="user"]') }).first();
  await viewport.evaluate(element => { element.scrollTop = 0; });
  await expect(editor).toHaveCount(0);
  await viewport.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect(page.getByLabel("编辑消息")).toContainText("写了很久的改动");
  await page.getByLabel("编辑消息").click();
  await page.keyboard.press("End");
  await page.keyboard.type("再加一句");
  await expect(page.getByLabel("编辑消息")).toContainText("写了很久的改动再加一句");
});
