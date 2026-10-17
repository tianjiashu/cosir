import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import { expect, test } from "@playwright/test";

let server: ChildProcess;
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
    [path.join(backend, "tests/fixtures/workspace_files_server.py")],
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
          return (
            await request.get("http://127.0.0.1:8766/__test__/files/health")
          ).ok();
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
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await page.route("**/workspaces/*/files/**", (route) =>
    route.continue({
      url: route.request().url().replace("127.0.0.1:8000", "127.0.0.1:8766"),
    }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "工作区 demo", exact: true }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface
    .getByRole("button", { name: "展开目录 alias", exact: true })
    .click();
  await expect(surface.getByText("不可展开", { exact: true })).toBeVisible();
  await surface.getByText("main.py", { exact: true }).click();
  await expect(surface.locator(".cm-content")).toContainText("before update");
  await request.post("http://127.0.0.1:8766/__test__/files/update");
  await expect(surface.locator(".cm-content")).toContainText("after update", {
    timeout: 10_000,
  });
  await expect(surface.getByText("文件已更新", { exact: true })).toBeVisible();
  await request.post("http://127.0.0.1:8766/__test__/files/delete");
  await expect(surface.getByRole("alert")).toContainText(
    "文件或工作区已不存在",
  );
  await request.post("http://127.0.0.1:8766/__test__/files/update");
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
  await page.getByRole("button", { name: "关闭 文件", exact: true }).click();
  await expect(surface).toHaveCount(0);
});

test("真实后端：Markdown 相对图片更新、大文件从末尾反向阅读和监听释放", async ({
  page,
  request,
}) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await page.route("**/workspaces/*/files/**", (route) =>
    route.continue({
      url: route.request().url().replace("127.0.0.1:8000", "127.0.0.1:8766"),
    }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "工作区 demo", exact: true }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByText("README.md", { exact: true }).click();
  await surface.getByLabel("Markdown 视图").selectOption("preview");
  const asset = surface.getByRole("img", { name: "asset", exact: true });
  await expect(asset).toBeVisible();
  await expect
    .poll(() => asset.evaluate((img: HTMLImageElement) => img.naturalWidth))
    .toBe(20);
  await request.post("http://127.0.0.1:8766/__test__/files/image-update");
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
  await page.getByRole("button", { name: "关闭 文件", exact: true }).click();
  await expect
    .poll(
      async () =>
        (
          await (
            await request.get(
              "http://127.0.0.1:8766/__test__/files/watch-status",
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
  await page.route(/\/workspaces\/\d+\/files\//, (route) =>
    route.continue({
      url: route.request().url().replace("127.0.0.1:8000", "127.0.0.1:8766"),
    }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "工作区 demo", exact: true }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  return page.getByTestId("workspace-files-surface");
}

test("引用：片段原文、打开来源、删除来源、拒绝发送保留胶囊与撤销", async ({
  page,
  request,
}) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await request.post("http://127.0.0.1:8766/__test__/files/update");
  const surface = await referenceWorkspace(page);
  await surface
    .getByRole("button", { name: "展开目录 src", exact: true })
    .click();
  await surface.getByRole("treeitem", { name: "main.py", exact: true }).click();
  await surface.getByRole("button", { name: "更多引用方式", exact: true }).click();
  await page
    .getByRole("button", { name: "引用行范围", exact: true })
    .click();
  await page
    .getByRole("button", { name: "添加行范围", exact: true })
    .click();
  await page.keyboard.press("Escape");
  const input = page.getByLabel("新对话内容");
  const capsule = input.locator("[data-workspace-reference]");
  await expect(capsule).toHaveCount(1);
  await expect(capsule).toContainText("main.py · L1–1");
  await capsule.getByRole("button").first().click();
  await expect(surface).toContainText("内容保存在消息正文中");
  await surface
    .getByRole("button", { name: "打开当前文件", exact: true })
    .click();
  await expect(surface.locator(".cm-content")).toContainText("after update");
  await request.post("http://127.0.0.1:8766/__test__/files/delete");
  await capsule.getByRole("button").first().click();
  await expect(surface.locator(".cm-content")).toContainText("after update");
  const sent: unknown[] = [];
  await page.route("**/assistant", (route) => {
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
  expect(JSON.stringify(sent)).toContain("cosir-snippet:src/main.py#L1-L1");
  expect(JSON.stringify(sent)).toContain("after update");
  expect(JSON.stringify(sent)).not.toContain(
    "application/vnd.cosir.workspace-reference",
  );
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
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await request.post("http://127.0.0.1:8766/__test__/files/update");
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

test("引用：Markdown 粘贴、普通消息发送、重载历史及编辑重跑", async ({
  page,
  request,
}) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await referenceWorkspace(page);
  const text =
    '请检查 [src/](cosir-dir:src/) [main.py](cosir-file:src/main.py)\n\n[main.py](cosir-snippet:src/main.py#L1-L2)\n\n```py\nprint("saved content")\n# [[cosir-file:example]] [[cosir-skill:example]]\n```\n\n';
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
  const sent: Array<{
    commands: Array<{
      sourceId?: string;
      message: {
        parts: Array<{ type: string; text?: string }>;
        attachments?: unknown[];
      };
    }>;
  }> = [];
  page.on("request", (req) => {
    if (req.url().endsWith("/assistant") && req.method() === "POST")
      sent.push(req.postDataJSON());
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
    sent[0].commands[0].message.parts.every((part) => part.type === "text"),
  ).toBe(true);
  expect(sent[0].commands[0].message.attachments).toBeUndefined();
  const sentText = sent[0].commands[0].message.parts
    .map((part) => part.text)
    .join("");
  expect(sentText).toContain("cosir-snippet:src/main.py#L1-L2");
  expect(sentText).toContain(
    "# [[cosir-file:example]] [[cosir-skill:example]]",
  );
  const user = page.locator('[data-role="user"]').last();
  await expect(user.locator("[data-workspace-reference]")).toHaveCount(3);
  await page.reload();
  await expect(user.locator("[data-workspace-reference]")).toHaveCount(3);
  await user.hover();
  await user.getByRole("button", { name: "编辑并重跑" }).click();
  const edit = page.getByRole("textbox", { name: "编辑消息" });
  await expect(edit.locator("[data-workspace-reference]")).toHaveCount(3);
  await page.getByRole("button", { name: "重跑", exact: true }).click();
  await expect.poll(() => sent.length).toBe(2);
  expect(sent[1].commands[0].sourceId).toBeTruthy();
  expect(
    sent[1].commands[0].message.parts
      .map((part) => part.text)
      .join("")
      .trimEnd(),
  ).toBe(sentText.trimEnd());
  await expect(user.locator("[data-workspace-reference]")).toHaveCount(3);
});

test("引用：Task 混合草稿切换后恢复附件并可移除", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await request.post("http://127.0.0.1:8000/__test__/seed-task", {
    data: { taskId: 100, title: "混合草稿甲" },
  });
  await request.post("http://127.0.0.1:8000/__test__/seed-task", {
    data: { taskId: 101, title: "混合草稿乙" },
  });
  await page.goto("/tasks/100");
  const input = page.getByLabel("消息输入");
  await input.focus();
  await input.evaluate((element) => {
    const data = new DataTransfer();
    data.setData("text/plain", "草稿甲 [main.py](cosir-file:src/main.py)");
    element.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData: data,
        bubbles: true,
        cancelable: true,
      }),
    );
  });
  // 仅替代操作系统文件选择边界，附件登记、AUI 适配器和草稿恢复仍走实际实现。
  await page.evaluate(() => {
    Object.assign(window, {
      __TAURI_INTERNALS__: {
        invoke: async (command: string) => {
          if (command === "plugin:dialog|open") return ["/tmp/notes.md"];
          if (command === "resolve_selected_attachment_path")
            return "/tmp/notes.md";
          if (command === "write_frontend_log") return;
          throw new Error(`Unexpected test IPC: ${command}`);
        },
      },
    });
  });
  await page
    .getByRole("button", { name: "添加图片、文件或文件夹", exact: true })
    .click();
  await page
    .getByRole("button", { name: "选择文件或图片", exact: true })
    .click();
  await page.keyboard.press("Escape");
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

test("引用：目录与文件通过右键添加、行号选区附带问题", async ({
  page,
  request,
}) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await request.post("http://127.0.0.1:8766/__test__/files/update");
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
  await expect(
    input.locator('[data-workspace-reference="directory"]'),
  ).toHaveCount(1);
  await expect(input.locator('[data-workspace-reference="file"]')).toHaveCount(
    1,
  );
  // 树行菜单操作后文件已激活，窄工作台需返回目录再选择下一个文件。
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface
    .getByRole("button", { name: "展开目录 src", exact: true })
    .click();
  await surface.getByRole("treeitem", { name: "main.py", exact: true }).click();
  await surface
    .locator(".cm-lineNumbers .cm-gutterElement")
    .filter({ hasText: /^1$/ })
    .click();
  await surface.getByRole("button", { name: "更多引用方式", exact: true }).click();
  await page.getByRole("button", { name: "解释", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(
    input.locator('[data-workspace-reference="snippet"]'),
  ).toContainText("L1–1");
  await expect(input).toContainText("请解释这段内容。");
});

for (const theme of ["light", "dark"]) {
  test(`浏览器忽略：${theme} 右键、撤销、恢复、阅读保留与 @ 隔离`, async ({
    page,
    request,
  }, testInfo) => {
    await request.post("http://127.0.0.1:8000/__test__/reset");
    const visibilityUrl = "http://127.0.0.1:8766/workspaces/1/files/visibility";
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
  await request.post("http://127.0.0.1:8000/__test__/reset");
  const api = "http://127.0.0.1:8766/workspaces/1/files/visibility";
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
