import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";
import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { installFakeDesktopHost } from "./fake-desktop-host";
import { API_ORIGIN, REAL_BACKEND_ORIGIN as REAL_BACKEND, REAL_BACKEND_PORT } from "./e2e-env";

let server: ChildProcess;
// 真实后端夹具的工作区 id 由 SQLite 自增分配，与前端内存测试服务的
// 固定 workspace id 无关；beforeAll 从夹具 health 读取，文件请求改写时统一映射。
let realWorkspaceId: number;

/** 把前端发往内存测试服务的文件接口改写到真实后端夹具，并映射工作区 id。 */
async function routeFilesToRealBackend(page: import("@playwright/test").Page) {
  await page.route(/\/workspaces\/\d+\/files\//, (route) =>
    route.continue({
      url: route
        .request()
        .url()
        .replace(
          new RegExp(`^${API_ORIGIN}/workspaces/\\d+/`),
          `${REAL_BACKEND}/workspaces/${realWorkspaceId}/`,
        ),
    }),
  );
}
test.beforeAll(async ({ request }) => {
  const backend = path.resolve(process.cwd(), "../backend");
  const python = path.join(
    backend,
    process.platform === "win32"
      ? ".venv/Scripts/python.exe"
      : ".venv/bin/python",
  );
  server = spawn(
    python,
    [path.join(backend, "tests/fixtures/workspace_files_server.py"), String(REAL_BACKEND_PORT)],
    { cwd: backend, stdio: "pipe" },
  );
  let output = "";
  server.stderr?.on("data", (chunk) => {
    output += String(chunk);
  });
  await expect
    .poll(
      async () => {
        if (server.exitCode !== null) throw new Error(output);
        try {
          const response = await request.get(
            `${REAL_BACKEND}/__test__/files/health`,
          );
          if (!response.ok()) return false;
          realWorkspaceId = (await response.json()).workspace_id;
          return true;
        } catch {
          return false;
        }
      },
      { timeout: 20_000 },
    )
    .toBe(true);
});
test.afterAll(async () => {
  if (server && server.exitCode === null) {
    server.kill("SIGTERM");
    await new Promise<void>((resolve) => server.once("exit", () => resolve()));
  }
});

test("真实后端：内部链接、原子替换实时更新、搜索和退出", async ({
  page,
  request,
}) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await routeFilesToRealBackend(page);
  await page.goto("/");
  // 行内「+」只在悬停时出现，先悬停工作区行。
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建对话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface
    .getByRole("button", { name: "展开目录 alias", exact: true })
    .click();
  await expect(surface.getByText("不可展开", { exact: true })).toBeVisible();
  await surface.getByText("main.py", { exact: true }).click();
  await expect(surface.locator(".cm-content")).toContainText("before update");
  await request.post(`${REAL_BACKEND}/__test__/files/update`);
  await expect(surface.locator(".cm-content")).toContainText("after update", {
    timeout: 10_000,
  });
  await expect(surface.getByText("文件已更新", { exact: true })).toBeVisible();
  await request.post(`${REAL_BACKEND}/__test__/files/delete`);
  // 真实后端返回 FILE_NOT_FOUND：中性的「文件不存在」而不是错误；文件恢复后由监听自动刷新，无需重新读取。
  await expect(surface.getByText("文件不存在", { exact: true })).toBeVisible();
  await expect(surface.getByRole("alert")).toHaveCount(0);
  await request.post(`${REAL_BACKEND}/__test__/files/update`);
  await expect(surface.locator(".cm-content")).toContainText("after update", {
    timeout: 10000,
  });
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface
    .getByRole("textbox", { name: "搜索工作区文件名" })
    .fill("src/main.py");
  await surface
    .getByRole("textbox", { name: "搜索工作区文件名" })
    .press("Enter");
  await expect(
    surface.getByRole("button", { name: /main.py.*src\/main.py/ }),
  ).toBeVisible();
  await page.getByRole("button", { name: "收起文件", exact: true }).click();
  await expect(surface).toHaveCount(0);
});

test("真实后端：Markdown 相对图片更新、大文件从末尾反向阅读和监听释放", async ({
  page,
  request,
}) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await routeFilesToRealBackend(page);
  await page.goto("/");
  // 行内「+」只在悬停时出现，先悬停工作区行。
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建对话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByText("README.md", { exact: true }).click();
  const asset = surface.getByRole("img", { name: "asset", exact: true });
  await expect(asset).toBeVisible();
  await expect
    .poll(() => asset.evaluate((img: HTMLImageElement) => img.naturalWidth))
    .toBe(20);
  await request.post(`${REAL_BACKEND}/__test__/files/image-update`);
  await expect
    .poll(() => asset.evaluate((img: HTMLImageElement) => img.naturalWidth), {
      timeout: 10000,
    })
    .toBe(40);
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface.getByText("large.txt", { exact: true }).click();
  await expect(surface.locator(".cm-content")).toContainText("中文😀 line");
  await surface.getByRole("button", { name: "末尾", exact: true }).click();
  const segment = surface.getByText(/分段阅读 ·/);
  await expect(segment).not.toContainText("· 0 /");
  const tail = await segment.textContent();
  await expect(
    surface.getByRole("button", { name: "上一段", exact: true }),
  ).toBeEnabled();
  await surface.getByRole("button", { name: "上一段", exact: true }).click();
  await expect(segment).not.toHaveText(tail!);
  await expect(surface.locator(".cm-content")).toContainText("中文😀 line");
  await page.getByRole("button", { name: "收起文件", exact: true }).click();
  await expect
    .poll(
      async () =>
        (
          await (
            await request.get(
              `${REAL_BACKEND}/__test__/files/watch-status`,
            )
          ).json()
        ).subscriptions,
    )
    .toBe(0);
});

async function referenceWorkspace(
  page: import("@playwright/test").Page,
  theme?: string,
) {
  await page.addInitScript((theme) => {
    localStorage.clear();
    if (theme) localStorage.setItem("cosir.theme-preference", theme);
    localStorage.setItem(
      "cosir:model-config-selection:workspace:7",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  }, theme);
  await routeFilesToRealBackend(page);
  await page.goto("/");
  // 行内「+」只在悬停时出现，先悬停工作区行。
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建对话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  return page.getByTestId("workspace-files-surface");
}

test("引用：行范围指针打开当前文件定位，文件删除后提示，拒绝发送保留胶囊与撤销", async ({
  page,
  request,
}) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${REAL_BACKEND}/__test__/files/update`);
  const surface = await referenceWorkspace(page);
  await surface
    .getByRole("button", { name: "展开目录 src", exact: true })
    .click();
  await surface.getByRole("treeitem", { name: "main.py", exact: true }).click();
  await surface
    .locator(".cm-lineNumbers .cm-gutterElement")
    .filter({ hasText: /^1$/ })
    .click();
  await surface.getByRole("button", { name: "添加选区到对话", exact: true }).click();
  const input = page.getByLabel("新对话内容");
  const capsule = input.locator("[data-workspace-reference]");
  await expect(capsule).toHaveCount(1);
  // 引用只是路径 + 行范围指针：不携带选取时的正文。
  await expect(capsule).toContainText("main.py");
  await expect(capsule).toContainText("L1");
  // 点击引用打开当前文件并定位到行，而不是展示消息里的片段快照。
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await capsule.getByRole("button").first().click();
  await expect(surface.locator(".cm-content")).toContainText("after update");
  await expect(surface.getByText("消息里的引用片段", { exact: false })).toHaveCount(0);
  // 文件被删后点击引用：提示不存在，不留下坏标签。
  await request.post(`${REAL_BACKEND}/__test__/files/delete`);
  await surface.getByRole("button", { name: "关闭标签" }).click();
  await capsule.getByRole("button").first().click();
  await expect(page.getByText("工作区里没有 src/main.py")).toBeVisible();
  const sent: unknown[] = [];
  await page.route("**/tasks/*/messages", (route) => {
    sent.push(route.request().postDataJSON());
    return route.fulfill({
      status: 409,
      json: {
        error: {
          code: "TASK_BUSY",
          message: "测试拒绝引用发送",
          retryable: true,
        },
      },
    });
  });
  await page.getByRole("button", { name: "开始对话", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("测试拒绝引用发送");
  await expect(capsule).toHaveCount(1);
  const sentBody = JSON.stringify(sent);
  expect(sentBody).toContain('<ref path=\\"src/main.py\\" lines=\\"1\\" />');
  expect(sentBody).not.toContain("after update");
  await page.keyboard.press("Escape");
  await capsule.getByRole("button", { name: /移除引用/ }).click();
  await expect(capsule).toHaveCount(0);
  await input.focus();
  const modifier = await page.evaluate(() =>
    /Mac/.test(navigator.platform) ? "Meta" : "Control",
  );
  await page.keyboard.press(`${modifier}+z`);
  await expect(capsule).toHaveCount(1);
  await page.screenshot({ path: "/private/tmp/cosir-reference-capsule.png" });
});

test("引用：@ 搜索去重、设置页隐藏文件并恢复草稿", async ({
  page,
  request,
}) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${REAL_BACKEND}/__test__/files/update`);
  await referenceWorkspace(page);
  const input = page.getByLabel("新对话内容");
  await input.fill("@src/main");
  await expect(
    page.getByRole("option", { name: "src/main.py", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(input.locator("[data-workspace-reference]")).toHaveCount(1);
  await input.press("End");
  await input.pressSequentially(" @src/main");
  await page.getByRole("option", { name: "src/main.py", exact: true }).click();
  await expect(input.locator("[data-workspace-reference]")).toHaveCount(1);
  await page.getByRole("button", { name: "系统配置", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "浏览工作区文件" }),
  ).toHaveCount(0);
  await expect(page.getByTestId("workspace-files-surface")).toHaveCount(0);
  await page.goBack();
  await expect(input.locator("[data-workspace-reference]")).toHaveCount(1);
});

test("引用：胶囊标签粘贴、普通消息发送、重载历史后撤回再发送", async ({
  page,
  request,
}) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await referenceWorkspace(page);
  const text =
    '请检查 <ref path="src/" /> <ref path="src/main.py" /> <ref path="src/main.py" lines="1-2" />\n\n```py\nprint("saved content")\n# <ref path="example.txt" /> <skill name="example" />\n```\n';
  const input = page.getByLabel("新对话内容");
  await input.focus();
  await input.evaluate((element, value) => {
    const data = new DataTransfer();
    data.setData("text/plain", value);
    element.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData: data,
        bubbles: true,
        cancelable: true,
      }),
    );
  }, text);
  await expect(input.locator("[data-workspace-reference]")).toHaveCount(3);
  // 正文里的三个 `<ref>` 变成胶囊；代码块里的同形标签保持字面量，不激活。
  await expect(input.locator("[data-inline-token]")).toHaveCount(3);
  const sent: Array<{
    url: string;
    message: {
      parts: Array<{ type: string; text?: string }>;
      attachments?: unknown[];
    };
  }> = [];
  page.on("request", (req) => {
    if (/\/tasks\/\d+\/messages$/.test(req.url()) && req.method() === "POST")
      sent.push({ url: req.url(), ...req.postDataJSON() });
  });
  await page.getByRole("button", { name: "开始对话", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/\d+/);
  await expect(
    page.getByText("streaming response", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "发送", exact: true }),
  ).toBeVisible();
  expect(
    sent[0].message.parts.every((part) => part.type === "text"),
  ).toBe(true);
  expect(sent[0].message.attachments).toBeUndefined();
  const sentText = sent[0].message.parts
    .map((part) => part.text)
    .join("");
  // 正文里的标签是规范形态的胶囊指针；代码块里的同形文本保持字面量。
  expect(sentText).toContain('<ref path="src/main.py" lines="1-2" />');
  expect(sentText).toContain(
    '# <ref path="example.txt" /> <skill name="example" />',
  );
  const user = page.locator('[data-role="user"]').last();
  await expect(user.locator("[data-workspace-reference]")).toHaveCount(3);
  await page.reload();
  await expect(user.locator("[data-workspace-reference]")).toHaveCount(3);
  await user.hover();
  await user.getByRole("button", { name: "撤回" }).click();
  // 撤回后引用原样回到主输入框，照常发送。
  await expect(page.getByLabel("消息输入").locator("[data-workspace-reference]")).toHaveCount(3);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => sent.length).toBe(2);
  expect(
    sent[1].message.parts
      .map((part) => part.text)
      .join("")
      .trimEnd(),
  ).toBe(sentText.trimEnd());
  await expect(user.locator("[data-workspace-reference]")).toHaveCount(3);
});

test("引用：Task 混合草稿切换后恢复附件并可移除", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, {
    data: { taskId: 100, title: "混合草稿甲" },
  });
  await request.post(`${API_ORIGIN}/__test__/seed-task`, {
    data: { taskId: 101, title: "混合草稿乙" },
  });
  await installFakeDesktopHost(page);
  await page.goto("/tasks/100");
  const input = page.getByLabel("消息输入");
  await input.focus();
  await input.evaluate((element) => {
    const data = new DataTransfer();
    data.setData("text/plain", '草稿甲 <ref path="src/main.py" />');
    element.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData: data,
        bubbles: true,
        cancelable: true,
      }),
    );
  });
  // 仅替代操作系统文件选择边界（fake-desktop-host），附件登记、AUI 适配器和草稿恢复仍走实际实现。
  await page.getByRole("button", { name: "添加", exact: true }).click();
  const chooser = page.waitForEvent("filechooser");
  await page.getByRole("option", { name: /^文件或图片/ }).click();
  await (await chooser).setFiles({ name: "notes.md", mimeType: "text/markdown", buffer: Buffer.from("# notes") });
  await expect(input).toContainText("notes.md");
  await page.getByText("混合草稿乙", { exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/101$/);
  await expect(input).toHaveText("");
  await input.fill("草稿乙");
  await page.getByText("混合草稿甲", { exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/100$/);
  await expect(input).toContainText("草稿甲");
  await expect(input.locator("[data-workspace-reference]")).toHaveCount(1);
  await expect(input).toContainText("notes.md");
  await expect(input).not.toContainText("已失效");
  await input
    .getByRole("button", { name: "移除附件 notes.md", exact: true })
    .click();
  await expect(input).not.toContainText("notes.md");
  await page.getByText("混合草稿乙", { exact: true }).click();
  await expect(input).toHaveText("草稿乙");
});

test("引用：目录与文件通过右键添加、选中文本可附带问题", async ({
  page,
  request,
}) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${REAL_BACKEND}/__test__/files/update`);
  const surface = await referenceWorkspace(page);
  await surface
    .getByText("src", { exact: true })
    .click({ button: "right" });
  await page.getByRole("menuitem", { name: "添加到对话" }).click();
  await surface
    .getByRole("treeitem", { name: "README.md", exact: true })
    .click({ button: "right" });
  await page.getByRole("menuitem", { name: "添加到对话" }).click();
  const input = page.getByLabel("新对话内容");
  const capsules = input.locator("[data-workspace-reference]");
  await expect(capsules).toHaveCount(2);
  // 目录引用以 `/` 结尾，文件是裸路径：都是同一种 `<ref>` 指针。
  await expect(capsules.filter({ hasText: "src/" })).toHaveCount(1);
  await expect(capsules.filter({ hasText: "README.md" })).toHaveCount(1);
  // 树行菜单操作后文件已激活，窄文件面板需返回目录再选择下一个文件。
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface
    .getByRole("button", { name: "展开目录 src", exact: true })
    .click();
  await surface
    .getByRole("treeitem", { name: "main.py", exact: true }).click();
  await surface
    .locator(".cm-lineNumbers .cm-gutterElement")
    .filter({ hasText: /^1$/ })
    .click({ button: "right" });
  await page.getByRole("menuitem", { name: "解释", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(input.locator("[data-workspace-reference]")).toHaveCount(3);
  await expect(capsules.filter({ hasText: "main.py" })).toContainText("L1");
  await expect(input).toContainText("请解释这段内容。");
});

for (const theme of ["light", "dark"]) {
  test(`浏览器忽略：${theme} 右键、撤销、恢复、阅读保留与 @ 隔离`, async ({
    page,
    request,
  }, testInfo) => {
    await request.post(`${API_ORIGIN}/__test__/reset`);
    const visibilityUrl = `${REAL_BACKEND}/workspaces/${realWorkspaceId}/files/visibility`;
    await request.patch(visibilityUrl, {
      data: {
        paths: ["src", "alias", "README.md", "src/main.py"],
        ignored: false,
      },
    });
    const surface = await referenceWorkspace(page, theme);
    await surface
      .getByRole("button", { name: "展开目录 src", exact: true })
      .click();
    await surface
      .getByRole("treeitem", { name: "main.py", exact: true })
      .dblclick();
    await expect(surface.locator(".cm-content")).toContainText("print(");
    await surface
      .getByRole("button", { name: "切换目录面板", exact: true })
      .click();
    const src = surface.getByText("src", { exact: true });
    await src.click({ button: "right" });
    await expect(
      page.getByRole("menuitem", { name: "忽略此目录" }),
    ).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath(`${theme}-ignore-menu.png`),
      animations: "disabled",
    });
    await page.getByRole("menuitem", { name: "忽略此目录" }).click();
    await expect(src).toHaveCount(0);
    await expect(
      surface.locator('[role="tab"]').filter({ hasText: "main.py" }),
    ).toHaveCount(1);
    await page.getByRole("button", { name: "撤销", exact: true }).click();
    await expect(src).toBeVisible();
    await src.click();
    await expect(
      surface.locator('[role="treeitem"]').filter({ has: page.getByText("src", { exact: true }) }),
    ).toBeFocused();
    await page.keyboard.press("Shift+F10");
    await page.getByRole("menuitem", { name: "忽略此目录" }).click();
    await expect(src).toHaveCount(0);
    const input = page.getByLabel("新对话内容");
    await input.fill("@src/main");
    await expect(
      page.getByRole("listbox", { name: "工作区引用候选" }),
    ).toContainText("没有匹配项");
    await expect(
      page.getByRole("option", { name: "src/main.py", exact: true }),
    ).toHaveCount(0);
    await input.press("Escape");
    const search = surface.getByRole("textbox", { name: "搜索工作区文件名" });
    await search.fill("src/main.py");
    await search.press("Enter");
    await expect(
      surface.getByText("没有匹配的文件", { exact: true }),
    ).toBeVisible();
    await surface.getByRole("button", { name: "文件浏览器更多选项" }).click();
    await page.getByRole("menuitem", { name: "管理忽略项" }).click();
    const dialog = page.getByRole("dialog", { name: "管理忽略项" });
    await expect(dialog.getByText("src", { exact: true })).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath(`${theme}-ignored-items.png`),
      animations: "disabled",
    });
    await dialog
      .getByRole("button", { name: "恢复显示 src", exact: true })
      .click();
    await expect(dialog.getByText("还没有忽略任何文件或目录")).toBeVisible();
    await dialog.getByRole("button", { name: "关闭", exact: true }).click();
    await expect(
      surface.getByRole("button", { name: /main.py.*src\/main.py/ }),
    ).toBeVisible();
    await input.click();
    await input.fill("");
    await input.pressSequentially("@src/main");
    await expect(
      page.getByRole("option", { name: "src/main.py", exact: true }),
    ).toBeVisible();
    await input.press("Escape");
    await surface
      .getByRole("button", { name: "切换目录面板", exact: true })
      .click();
    await expect(surface.locator(".cm-content")).toContainText("print(");
  });
}

test("浏览器忽略：多选、失败保留、空目录和旧搜索取消", async ({
  page,
  request,
}) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  const api = `${REAL_BACKEND}/workspaces/${realWorkspaceId}/files/visibility`;
  const surface = await referenceWorkspace(page);
  const src = surface.getByText("src", { exact: true });
  await src.click();
  await surface
    .getByText("alias", { exact: true })
    .click({ modifiers: ["ControlOrMeta"] });
  await src.click({ button: "right" });
  await page.getByRole("menuitem", { name: "忽略所选 2 项" }).click();
  await expect(src).toHaveCount(0);
  await expect(surface.getByText("alias", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect(src).toBeVisible();
  await page.route("**/files/visibility", async (route) => {
    if (route.request().method() === "PATCH")
      return route.fulfill({ status: 422, json: { detail: "写入失败" } });
    return route.fallback();
  });
  await src.click();
  await src.click({ button: "right" });
  await page.getByRole("menuitem", { name: "忽略此目录" }).click();
  await expect(page.getByText("忽略失败", { exact: true })).toBeVisible();
  await expect(src).toBeVisible();
  await page.unroute("**/files/visibility");
  // 请求已启动时通过设置 API 更新；点击刷新收敛版本，旧请求迟到不得重新展示。
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started = false;
  await page.route("**/files/search", async (route) => {
    if (started) return route.fallback();
    started = true;
    await gate;
    await route.fulfill({
      contentType: "application/x-ndjson",
      body:
        JSON.stringify({
          entries: [
            {
              path: "src/main.py",
              name: "main.py",
              kind: "file",
              symlink: false,
              hint: null,
            },
          ],
          skipped: 0,
          skipped_reasons: {},
          complete: true,
        }) + "\n",
    });
  });
  const search = surface.getByRole("textbox", { name: "搜索工作区文件名" });
  await search.fill("src/main.py");
  await search.press("Enter");
  await expect.poll(() => started).toBe(true);
  await request.patch(api, {
    data: { paths: ["src/main.py", "src/loop"], ignored: true },
  });
  await surface
    .getByRole("button", { name: "刷新工作区文件", exact: true })
    .click();
  await expect(
    surface.getByText("没有匹配的文件", { exact: true }),
  ).toBeVisible();
  release();
  await expect(
    surface.getByRole("button", { name: /main.py.*src\/main.py/ }),
  ).toHaveCount(0);
  await surface
    .getByRole("button", { name: "清空搜索，返回目录", exact: true })
    .click();
  await surface
    .getByRole("button", { name: "展开目录 src", exact: true })
    .click();
  await expect(
    surface.getByText("此目录中的内容已被忽略", { exact: true }),
  ).toBeVisible();
  await request.patch(api, {
    data: { paths: ["src/main.py", "src/loop"], ignored: false },
  });
  await surface
    .getByRole("button", { name: "刷新工作区文件", exact: true })
    .click();
  await expect(
    surface.getByRole("treeitem", { name: "main.py", exact: true }),
  ).toBeVisible();
  await search.fill("src/main.py");
  await search.press("Enter");
  const result = surface.getByRole("button", { name: /main.py.*src\/main.py/ });
  await result.click({ button: "right" });
  await page.getByRole("menuitem", { name: "忽略此文件" }).click();
  await expect(
    surface.getByText("没有匹配的文件", { exact: true }),
  ).toBeVisible();
  await request.patch(api, {
    data: { paths: ["src/main.py"], ignored: false },
  });
});


test("真实后端：文件地址与文件名查找分离，候选定位和字面路径", async ({ page, request }, testInfo) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${REAL_BACKEND}/__test__/files/link-fixture`);
  await routeFilesToRealBackend(page);
  await request.post(`${API_ORIGIN}/__test__/seed-tool-trace`);
  const state = await (await request.get(`${API_ORIGIN}/tasks/42/assistant/state`)).json() as TransportState;
  state.messages[1].parts = [{ type: "text", status: "completed", text:
    "确定地址 [API.md](docs/brand/API.md)，根文件 [root](API.md)。未知位置 `API.md:2`。文档 [guide](docs/brand/guide.md)。缺失 [gone](docs/brand/gone.md#L2)。[slow](slow.md) [slow-again](slow-again.md)。" }];
  await seedState(request, 42, state);
  await page.goto("/tasks/42");
  const message = page.locator('main [data-index="1"]');
  const surface = page.getByTestId("workspace-files-surface");
  await message.getByRole("link", { name: "API.md", exact: true }).click();
  await expect(surface).toContainText("BRAND_API");
  await expect(surface.getByText("选择文件以打开", { exact: true })).toHaveCount(0);
  await message.locator('[data-slot="workspace-file-mention"]').hover();
  await message.getByRole("button", { name: "查找文件 API.md", exact: true }).click();
  await expect(surface).toContainText("选择文件以打开");
  await expect(surface.getByRole("button", { name: "API.md docs/brand/API.md", exact: true })).toBeVisible();
  await expect(surface.getByRole("button", { name: "API.md node_modules/example/API.md", exact: true })).toBeVisible();
  await expect(surface).not.toContainText("ROOT_API");
  await page.screenshot({ path: testInfo.outputPath("file-lookup-light.png") });
  const searchInput = surface.getByRole("textbox", { name: "搜索工作区文件名" });
  await searchInput.fill("guide.md");
  await expect(surface.getByText("选择文件以打开", { exact: true })).toHaveCount(0);
  await searchInput.press("Enter");
  await expect(surface.getByRole("button", { name: "guide.md docs/brand/guide.md", exact: true })).toBeVisible();
  await message.getByRole("button", { name: "查找文件 API.md", exact: true }).click();

  await surface.getByRole("button", { name: "API.md docs/brand/API.md", exact: true }).click();
  const editor = surface.getByRole("textbox", { name: "只读文件 API.md" });
  await expect(editor).toBeVisible();
  await expect(surface.getByRole("button", { name: "添加选区到对话", exact: true })).toBeVisible();
  await editor.focus();
  await page.keyboard.press("ControlOrMeta+l");
  await expect(page.getByRole("textbox", { name: "消息输入" })).toContainText("L2");
  await message.getByRole("link", { name: "root", exact: true }).click();
  await expect(surface).toContainText("ROOT_API");
  await message.getByRole("link", { name: "guide", exact: true }).click();
  await surface.getByRole("button", { name: "./API.md", exact: true }).click();
  await expect(surface).toContainText("BRAND_API");
  await message.getByRole("link", { name: "guide", exact: true }).click();
  await surface.getByRole("link", { name: "special", exact: true }).click();
  await expect(surface).toContainText("LITERAL_PATH");
  await message.getByRole("link", { name: "gone", exact: true }).click();
  await expect(page.getByText("工作区里没有 docs/brand/gone.md", { exact: true })).toBeVisible();
  await expect(surface.getByRole("tab", { name: "gone.md", exact: true })).toHaveCount(0);
  await page.emulateMedia({ colorScheme: "dark" });
  await message.locator('[data-slot="workspace-file-mention"]').hover();
  await page.screenshot({ path: testInfo.outputPath("file-links-dark.png"), animations: "disabled" });
  // 慢文件 info 使用真实后端响应，只延迟网络放行，验证用户后续操作的优先级。
  let release!: () => void;
  await page.route("**/files/info?*", async (route) => {
    const file = new URL(route.request().url()).searchParams.get("path");
    if (file === "slow.md" || file === "slow-again.md") {
      await new Promise<void>((resolve) => { release = resolve; });
    }
    await route.fallback();
  });
  const slowRequest = page.waitForRequest((r) => r.url().includes("/files/info?") && new URL(r.url()).searchParams.get("path") === "slow.md");
  await message.getByRole("link", { name: "slow", exact: true }).click();
  await slowRequest;
  await expect.poll(() => typeof release).toBe("function");
  await surface.getByRole("tab", { selected: true }).click();
  const slowResponse = page.waitForResponse((r) => r.url().includes("/files/info?") && new URL(r.url()).searchParams.get("path") === "slow.md");
  release();
  await slowResponse;
  await expect(surface).toContainText("LITERAL_PATH");
  await expect(surface.getByRole("tab", { name: "slow.md", exact: true })).toHaveCount(0);
  const nextRequest = page.waitForRequest((r) => r.url().includes("/files/info?") && new URL(r.url()).searchParams.get("path") === "slow-again.md");
  await message.getByRole("link", { name: "slow-again", exact: true }).click();
  await nextRequest;
  await surface.getByRole("button", { name: "收起文件", exact: true }).click();
  const nextResponse = page.waitForResponse((r) => r.url().includes("/files/info?") && new URL(r.url()).searchParams.get("path") === "slow-again.md");
  release();
  await nextResponse;
  await expect(surface).toHaveCount(0);

});
