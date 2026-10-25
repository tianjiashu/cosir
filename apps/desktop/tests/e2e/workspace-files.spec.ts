import { expect, test, type Page } from "@playwright/test";
import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";

const entry = (path: string, kind = "file") => ({
  path,
  name: path.split("/").at(-1),
  kind,
  symlink: false,
  hint: null,
});
const source = "# preview comment\ndef greet():\n    return '只读文件预览'\n";

async function mockFiles(page: Page) {
  const requests: string[] = [];
  await page.route("**/workspaces/*/files/**", async (route) => {
    const url = new URL(route.request().url());
    requests.push(`${route.request().method()} ${url.pathname}`);
    const operation = url.pathname.split("/").at(-1);
    const path = url.searchParams.get("path") ?? "";
    if (operation === "visibility") return route.fulfill({ json: { ignored_paths: [], revision: "empty" } });
    if (operation === "directory")
      return route.fulfill({
        json: {
          path,
          entries:
            path === ""
              ? [
                  entry("src", "directory"),
                  entry("main.py"),
                  entry("README.md"),
                  entry("photo.png"),
                  entry("document.pdf"),
                ]
              : [entry("src/nested.json")],
          next_cursor: null,
          total: 5,
        },
      });
    if (operation === "info")
      return route.fulfill({
        json: {
          path,
          name: path.split("/").at(-1),
          root_path: "/workspace/demo",
          size: source.length,
          modified_ns: 1_700_000_000_000_000_000,
          version: "v1",
          kind: path.endsWith(".png")
            ? "image"
            : path.endsWith(".pdf")
              ? "pdf"
              : path.endsWith(".md")
                ? "markdown"
                : "text",
          mime: "text/plain",
          encoding: "utf-8",
        },
      });
    if (operation === "text")
      return route.fulfill({
        json: {
          text: path.endsWith(".json")
            ? '{"nested": true}'
            : path.endsWith(".md")
              ? "# Workspace preview\n\n[Python](main.py)"
              : source,
          offset: 0,
          next_offset: null,
          size: source.length,
          version: "v1",
          encoding: "utf-8",
        },
      });
    if (operation === "watch")
      return route.fulfill({
        contentType: "application/x-ndjson",
        body: '{"type":"ready","polling":false}\n',
      });
    if (operation === "find-text")
      return route.fulfill({
        contentType: "application/x-ndjson",
        body:
          JSON.stringify({
            matches: [{ offset: 0, snippet: "def greet():" }],
            complete: true,
          }) + "\n",
      });
    if (operation === "search")
      return route.fulfill({
        contentType: "application/x-ndjson",
        body:
          JSON.stringify({
            entries:
              route.request().postDataJSON().query === "missing"
                ? []
                : [entry("src/nested.json")],
            skipped: 0,
            skipped_reasons: {},
            complete: true,
          }) + "\n",
      });
    if (operation === "resource" && path.endsWith(".png"))
      return route.fulfill({
        contentType: "image/png",
        body: Buffer.from(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=",
          "base64",
        ),
      });
    if (operation === "resource") {
      const objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        "<< /Length 43 >>\nstream\nBT /F1 16 Tf 30 350 Td (PDF preview) Tj ET\nendstream",
      ];
      let pdf = "%PDF-1.4\n";
      const offsets = [0];
      objects.forEach((object, index) => {
        offsets.push(Buffer.byteLength(pdf));
        pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
      });
      const xref = Buffer.byteLength(pdf);
      pdf += `xref\n0 6\n0000000000 65535 f \n${offsets
        .slice(1)
        .map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`)
        .join(
          "",
        )}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
      return route.fulfill({ contentType: "application/pdf", body: pdf });
    }
    return route.fulfill({ status: 404, json: { detail: "not found" } });
  });
  return requests;
}

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
});

test("目录、只读预览、固定标签、未展开目录搜索与定位", async ({ page }) => {
  const requests = await mockFiles(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page.getByRole("button", { name: "工作区 demo", exact: true }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await expect(surface.getByText("main.py", { exact: true })).toBeVisible();
  await surface.getByRole("treeitem", { name: "main.py", exact: true }).click();
  await expect(surface.locator(".cm-content")).toContainText("只读文件预览");
  await expect(surface.locator(".cm-content")).toHaveAttribute(
    "contenteditable",
    "false",
  );
  await surface
    .getByRole("button", { name: "固定 main.py", exact: true })
    .click();
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface
    .getByRole("textbox", { name: "搜索工作区文件名" })
    .fill("nested");
  await surface.getByRole("textbox", { name: "搜索工作区文件名" }).press("Enter");
  await expect(surface.getByText("搜索完成", { exact: false })).toBeVisible();
  await surface.getByRole("button", { name: /nested.json/ }).click();
  await expect(surface.locator(".cm-content")).toContainText('"nested": true');
  await surface.getByRole("button", { name: "文件路径 src/nested.json", exact: true }).click();
  await page.getByRole("button", { name: "在目录中定位当前文件", exact: true }).click();
  await expect(
    surface.getByTestId("workspace-file-tree").getByText("nested.json"),
  ).toBeVisible();
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await expect(surface.getByRole("tab", { name: "main.py" })).toBeVisible();
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface.getByRole("button", { name: "全部折叠" }).click();
  await expect(
    surface.getByTestId("workspace-file-tree").getByText("nested.json"),
  ).toHaveCount(0);
  expect(errors).toEqual([]);
  expect(requests.filter((value) => /PUT|PATCH|DELETE/.test(value))).toEqual(
    [],
  );
  await page.screenshot({ path: "/private/tmp/cosir-workspace-files.png" });
});

test("Markdown、图片和 PDF.js 离线阅读器", async ({ page }) => {
  await mockFiles(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page.getByRole("button", { name: "工作区 demo", exact: true }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByText("README.md", { exact: true }).click();
  await surface.getByLabel("Markdown 视图").selectOption("preview");
  await expect(
    surface.getByRole("heading", { name: "Workspace preview" }),
  ).toBeVisible();
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface.getByText("photo.png", { exact: true }).click();
  await expect(surface.getByRole("img", { name: "photo.png" })).toBeVisible();
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface.getByText("document.pdf", { exact: true }).click();
  await expect(surface.locator(".pdfViewer canvas")).toBeVisible({
    timeout: 20_000,
  });
  await expect(surface.getByText("PDF preview", { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
  await page.screenshot({ path: "/private/tmp/cosir-workspace-pdf.png" });
});

test("阅读优先、每文件偏好、标签键盘和搜索恢复", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await mockFiles(page);
  await page.goto("/");
  await page.getByRole("button", { name: "工作区 demo", exact: true }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByText("README.md", { exact: true }).click();
  await expect(
    surface.getByRole("heading", { name: "Workspace preview" }),
  ).toBeVisible();
  await expect(surface.getByRole("textbox", { name: "搜索工作区文件名" })).toHaveCount(0);
  await surface
    .getByRole("button", { name: "固定 README.md", exact: true })
    .click();
  await surface.getByLabel("Markdown 视图").selectOption("source");
  await surface.getByRole("button", { name: "更多阅读选项", exact: true }).click();
  await page.getByRole("button", { name: "自动换行", exact: true }).click();
  await page.keyboard.press("Escape");
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface.getByRole("treeitem", { name: "main.py", exact: true }).click();
  const code = surface.locator(".cm-editor");
  await expect(code).toBeVisible();
  expect((await code.boundingBox())!.height).toBeGreaterThan(350);
  const codeTab = surface.getByRole("tab", { name: "main.py", exact: true });
  await codeTab.focus();
  await codeTab.press("ArrowLeft");
  const markdownTab = surface.getByRole("tab", {
    name: "README.md",
    exact: true,
  });
  await expect(markdownTab).toBeFocused();
  await expect(markdownTab).toHaveAttribute("aria-selected", "true");
  await expect(surface.getByLabel("Markdown 视图")).toHaveValue("source");
  await surface.getByRole("button", { name: "更多阅读选项", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "自动换行", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.keyboard.press("Escape");
  await markdownTab.press("End");
  await expect(codeTab).toHaveAttribute("aria-selected", "true");
  await expect(codeTab).toBeFocused();
  await page.keyboard.press("ControlOrMeta+f");
  await expect(surface.getByLabel("全文件查找")).toBeFocused();
  await expect(
    surface.getByRole("button", { name: "停止", exact: true }),
  ).toHaveCount(0);
  await surface.getByLabel("全文件查找").fill("greet");
  await surface.getByLabel("全文件查找").press("Enter");
  await expect(surface.getByText(/查找完成/)).toBeVisible();
  await expect(
    surface.getByRole("button", { name: "停止", exact: true }),
  ).toHaveCount(0);
  await surface.getByRole("button", { name: "收起查找", exact: true }).click();
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface.getByRole("textbox", { name: "搜索工作区文件名" }).fill("missing");
  await surface.getByRole("textbox", { name: "搜索工作区文件名" }).press("Enter");
  await expect(
    surface.getByText("没有匹配的文件", { exact: true }),
  ).toBeVisible();
  await expect(
    surface.getByRole("button", { name: "停止", exact: true }),
  ).toHaveCount(0);
  await expect(
    surface.getByRole("button", { name: "下一页", exact: true }),
  ).toHaveCount(0);
  await surface
    .getByRole("button", { name: "清空文件名搜索", exact: true })
    .click();
  await expect(
    surface.getByRole("treeitem", { name: "README.md", exact: true }),
  ).toBeVisible();
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await page.screenshot({ path: "/private/tmp/cosir-files-reading-fixed.png" });
  await surface
    .getByRole("button", { name: "关闭文件 main.py", exact: true })
    .click();
  await expect(markdownTab).toBeFocused();
  await surface.getByLabel("Markdown 视图").selectOption("preview");
  await surface.getByRole("heading", { name: "Workspace preview" }).click();
  await page.keyboard.press("ControlOrMeta+f");
  await expect(surface.getByLabel("全文件查找")).toBeFocused();
  await surface
    .getByRole("button", { name: "关闭文件 README.md", exact: true })
    .click();
  await expect(surface.getByRole("textbox", { name: "搜索工作区文件名" })).toBeFocused();
});

for (const theme of ["light", "dark"]) {
  test(`${theme} 代码对比度、宽面板与缩放布局`, async ({ page }) => {
    await page.addInitScript(
      (value) => localStorage.setItem("cosir.theme-preference", value),
      theme,
    );
    await mockFiles(page);
    await page.goto("/");
    await page
      .getByRole("button", { name: "工作区 demo", exact: true })
      .click();
    await page.getByRole("button", { name: "浏览工作区文件" }).click();
    const surface = page.getByTestId("workspace-files-surface");
    await surface
      .getByRole("treeitem", { name: "main.py", exact: true })
      .click();
    await expect(surface.locator(".cm-line span").first()).toBeVisible();
    const colors = await surface
      .locator(".cm-line span")
      .evaluateAll((elements) =>
        elements.map((element) => getComputedStyle(element).color),
      );
    const luminance = (color: string) => {
      const values = color
        .match(/[\d.]+/g)!
        .slice(0, 3)
        .map(Number)
        .map((value) => {
          const v = value / 255;
          return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
        });
      return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
    };
    const background = theme === "dark" ? luminance("rgb(10,10,10)") : 1;
    for (const color of colors) {
      const foreground = luminance(color);
      expect(
        (Math.max(background, foreground) + 0.05) /
          (Math.min(background, foreground) + 0.05),
        color,
      ).toBeGreaterThanOrEqual(4.5);
    }
    // 为主区留足空间后，通过实际可用宽度验证并排阅读断点。
    await page.setViewportSize({ width: 1600, height: 900 });
    const outer = page.getByRole("separator", { name: "调整工作台宽度" });
    await expect(outer).toHaveAttribute("aria-valuemax", "900");
    await outer.focus();
    await outer.press("End");
    await expect(surface.getByRole("textbox", { name: "搜索工作区文件名" })).toBeVisible();
    await expect(surface.locator(".cm-content")).toBeVisible();
    await expect(
      surface.getByRole("separator", { name: "调整文件目录宽度" }),
    ).toBeVisible();
    await page.screenshot({
      path: `/private/tmp/cosir-files-${theme}-wide-fixed.png`,
    });
    // 桌面最小窗口；正文仍可展开，无需通过移动端布局规避。
    await page.setViewportSize({ width: 960, height: 640 });
    await expect(surface.locator(".cm-content")).toBeVisible();
  });
}

test("搜索停止重试、未扫描原因与编码变更取消查找", async ({ page }) => {
  await mockFiles(page);
  let releaseSearch!: () => void;
  const searchGate = new Promise<void>((resolve) => {
    releaseSearch = resolve;
  });
  let searchCount = 0;
  await page.route("**/files/search", async (route) => {
    searchCount += 1;
    await searchGate;
    await route.fulfill({
      contentType: "application/x-ndjson",
      body:
        JSON.stringify({
          entries: [entry("main.py")],
          complete: true,
          skipped: 1,
          skipped_reasons: { 符号链接指向工作区外部: 1 },
        }) + "\n",
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "工作区 demo", exact: true }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByRole("textbox", { name: "搜索工作区文件名" }).fill("main");
  await surface.getByRole("textbox", { name: "搜索工作区文件名" }).press("Enter");
  await expect.poll(() => searchCount).toBeGreaterThan(0);
  await surface.getByRole("button", { name: "停止", exact: true }).click();
  await expect(surface.getByText(/已停止/)).toBeVisible();
  releaseSearch();
  await surface.getByRole("button", { name: "重新搜索", exact: true }).click();
  await expect(surface.getByText(/搜索完成/)).toBeVisible();
  await expect(
    surface.getByRole("button", { name: "停止", exact: true }),
  ).toHaveCount(0);
  await surface.getByText("查看未扫描原因", { exact: true }).click();
  await expect(
    surface.getByText("符号链接指向工作区外部：1 项", { exact: true }),
  ).toBeVisible();
  await surface
    .getByRole("button", { name: "main.py main.py", exact: true })
    .click();
  await expect(surface.locator(".cm-content")).toBeVisible();
  let releaseFind!: () => void;
  const findGate = new Promise<void>((resolve) => {
    releaseFind = resolve;
  });
  let findStarted = false;
  let findDelivered = false;
  await page.route("**/files/find-text", async (route) => {
    findStarted = true;
    await findGate;
    await route.fulfill({
      contentType: "application/x-ndjson",
      body:
        JSON.stringify({
          matches: [{ offset: 0, snippet: "旧编码迟到结果" }],
          complete: true,
        }) + "\n",
    });
    findDelivered = true;
  });
  await surface
    .getByRole("button", { name: "查找文件内容", exact: true })
    .click();
  await surface.getByLabel("全文件查找").fill("greet");
  await surface.getByLabel("全文件查找").press("Enter");
  await expect.poll(() => findStarted).toBe(true);
  await surface.getByRole("button", { name: "更多阅读选项", exact: true }).click();
  await page.getByLabel("文本编码").selectOption("cp1252");
  await page.keyboard.press("Escape");
  await expect(
    surface.getByRole("button", { name: "停止", exact: true }),
  ).toHaveCount(0);
  releaseFind();
  await expect.poll(() => findDelivered).toBe(true);
  await expect(surface.getByText(/查找完成/)).toHaveCount(0);
  await expect(
    surface.getByText("旧编码迟到结果", { exact: true }),
  ).toHaveCount(0);
});

test("阅读工具栏在窄宽度保持单行，菜单与搜索焦点独立", async ({ page }, testInfo) => {
  await mockFiles(page);
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await page.getByRole("button", { name: "工作区 demo", exact: true }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByRole("treeitem", { name: "README.md", exact: true }).click();
  for (const width of [320, 480, 650]) {
    await page.evaluate(async width => {
      const modulePath = "/lib/workbench/store.ts";
      const { useWorkbenchStore } = await import(modulePath);
      useWorkbenchStore.getState().setPanelWidth(width);
    }, width);
    const toolbar = surface.getByLabel("文件阅读工具栏");
    await expect(toolbar).toBeVisible();
    await expect.poll(() => toolbar.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
    const bounds = await toolbar.boundingBox();
    expect(bounds!.height).toBeLessThan(46);
    const body = await surface.getByRole("heading", { name: "Workspace preview" }).boundingBox();
    const panel = await page.getByRole("complementary", { name: "工作台" }).boundingBox();
    expect(body!.y - panel!.y).toBeLessThan(160);
    await page.screenshot({ path: testInfo.outputPath(`reading-${width}.png`) });
  }
  await surface.getByRole("button", { name: "查找文件内容", exact: true }).click();
  await surface.getByRole("button", { name: "更多阅读选项", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("文本编码")).toBeHidden();
  await expect(surface.getByLabel("全文件查找")).toBeVisible();
  await surface.getByLabel("全文件查找").focus();
  await page.keyboard.press("Escape");
  await expect(surface.getByRole("button", { name: "查找文件内容", exact: true })).toBeFocused();
  await surface.getByLabel("Markdown 视图").selectOption("source");
  await surface.getByRole("button", { name: "查找文件内容", exact: true }).click();
  await surface.locator(".cm-content").click();
  await page.keyboard.press("Escape");
  await expect(surface.getByLabel("全文件查找")).toBeHidden();
  await page.evaluate(async () => {
    const modulePath = "/lib/workbench/store.ts";
    (await import(modulePath)).useWorkbenchStore.getState().setPanelWidth(480);
  });
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await expect(surface.getByRole("textbox", { name: "搜索工作区文件名" })).toBeFocused();
});

test("同名标签消歧、溢出菜单、临时替换和关闭后的目录入口", async ({ page }) => {
  await mockFiles(page);
  await page.goto("/");
  await page.getByRole("button", { name: "工作区 demo", exact: true }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await page.evaluate(async () => {
    const modulePath = "/lib/workspace-files/store.ts";
    const store = (await import(modulePath)).useFilesStore.getState();
    for (const path of ["src/index.ts", "test/index.ts", ...Array.from({length: 12}, (_, i) => `deep/folder/file-${i}.ts`)]) store.open(7, path, true);
    store.open(7, "temporary.ts");
    store.open(7, "replacement.ts");
  });
  await expect(surface.getByRole("tab", { name: "temporary.ts", exact: true })).toHaveCount(0);
  await expect(surface.getByRole("tab", { name: "replacement.ts", exact: true })).toHaveAttribute("aria-selected", "true");
  await surface.getByRole("button", { name: "全部文件", exact: true }).click();
  await page.getByRole("menuitemradio", { name: "test/index.ts", exact: true }).click();
  await expect(surface.getByRole("tab", { name: "test/index.ts", exact: true })).toHaveAttribute("aria-selected", "true");
  const active = surface.getByRole("tab", { name: "test/index.ts", exact: true });
  const list = surface.getByRole("tablist", { name: "文件预览标签" });
  expect((await active.boundingBox())!.x).toBeGreaterThanOrEqual((await list.boundingBox())!.x);
  await expect(surface.getByRole("tab", { name: "src/index.ts", exact: true })).toHaveCount(1);
  await page.evaluate(async () => {
    const modulePath = "/lib/workspace-files/store.ts";
    const store = (await import(modulePath)).useFilesStore.getState();
    store.patch(7, { treeVisible: false });
    for (const tab of store.sessions[7].tabs) store.close(7, tab.path);
  });
  await expect(surface.getByRole("textbox", { name: "搜索工作区文件名" })).toBeVisible();
});

test("文件菜单开关不锁定主对话，长消息滚动位置和节点保持稳定", async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockFiles(page);
  await request.post("http://127.0.0.1:8000/__test__/seed-tool-trace");
  const state = await (await request.get("http://127.0.0.1:8000/tasks/42/assistant/state")).json() as TransportState;
  state.messages[1].parts = Array.from({ length: 45 }, (_, i) => [
    { type: "reasoning" as const, text: `分析第 ${i} 次输出`, status: "completed" as const },
    { type: "text" as const, text: `第 ${i} 次读取终端输出。继续观察命令的执行过程，检查输出和状态是否符合预期。`, status: "completed" as const },
  ]).flat();
  await seedState(request, 42, state);
  await page.goto("/tasks/42");
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const trigger = page.getByRole("button", { name: "文件浏览器更多选项" });
  const viewport = page.locator(".aui-thread-root > div");
  const row = page.locator('main [data-index="1"]');
  await expect(row).toContainText("第 44 次读取终端输出");
  await viewport.evaluate((element) => { element.scrollTop = 1200; });
  await expect.poll(() => viewport.evaluate((element) => element.scrollTop)).toBe(1200);
  const originalRow = await row.elementHandle();
  const originalBounds = await viewport.boundingBox();

  for (let cycle = 0; cycle < 3; cycle++) {
    await trigger.click();
    await expect(page.getByRole("menuitem", { name: "管理忽略项" })).toBeVisible();
    // 普通面板菜单不能把整个工作台变成不可访问、不可点击的模态背景。
    await expect(page.locator("body")).not.toHaveAttribute("data-scroll-locked");
    await expect(page.locator("body")).not.toHaveCSS("pointer-events", "none");
    await expect(page.getByRole("main")).toBeVisible();
    await trigger.click();
    await expect(page.getByRole("menu")).toHaveCount(0);
    await expect(trigger).toBeFocused();
    expect(await viewport.boundingBox()).toEqual(originalBounds);
    expect(await viewport.evaluate((element) => element.scrollTop)).toBe(1200);
    expect(await originalRow!.evaluate((element) => element === document.querySelector('main [data-index="1"]'))).toBe(true);
  }

  await trigger.focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("menuitem", { name: "管理忽略项" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await trigger.click();
  await page.getByRole("menuitem", { name: "管理忽略项" }).click();
  await expect(page.getByRole("dialog", { name: "管理忽略项" })).toBeVisible();
  await expect(page.getByRole("main")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
