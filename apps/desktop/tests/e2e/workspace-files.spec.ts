import { expect, test, type Page } from "@playwright/test";
import { readState, seedState } from "./transport-fixtures";
import { API_ORIGIN } from "./e2e-env";

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
  await request.post(`${API_ORIGIN}/__test__/reset`);
});

test("目录、只读预览、固定标签、未展开目录搜索与定位", async ({ page }) => {
  const requests = await mockFiles(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  // 行内「+」只在悬停时出现，先悬停工作区行。
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await expect(surface.getByText("main.py", { exact: true })).toBeVisible();
  await surface.getByRole("treeitem", { name: "main.py", exact: true }).click();
  await expect(surface.locator(".cm-content")).toContainText("只读文件预览");
  await expect(surface.locator(".cm-content")).toHaveAttribute(
    "contenteditable",
    "false",
  );
  // 临时预览标签右键固定；固定后打开其他文件不再替换它。
  await surface.getByRole("tab", { name: "main.py", exact: true }).click({ button: "right" });
  await page.getByRole("menuitem", { name: "固定标签", exact: true }).click();
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
  // 行内「+」只在悬停时出现，先悬停工作区行。
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByText("README.md", { exact: true }).click();
  // Markdown 默认渲染，工具栏只有一个切到源码的开关。
  await expect(
    surface.getByRole("heading", { name: "Workspace preview" }),
  ).toBeVisible();
  await expect(surface.getByRole("button", { name: "查看源码", exact: true })).toBeVisible();
  // 大纲：渲染视图按标题上的源码行号滚动；源码视图选中对应行。单个标签放得下，不显示「全部文件」。
  await expect(surface.getByRole("button", { name: "全部文件", exact: true })).toHaveCount(0);
  await expect(surface.locator('[data-source-line="1"]')).toHaveText("Workspace preview");
  await surface.getByRole("button", { name: "大纲", exact: true }).click();
  await page.getByRole("navigation", { name: "文档大纲" }).getByRole("button", { name: "Workspace preview" }).click();
  await surface.getByRole("button", { name: "查看源码", exact: true }).click();
  await surface.getByRole("button", { name: "大纲", exact: true }).click();
  await page.getByRole("navigation", { name: "文档大纲" }).getByRole("button", { name: "Workspace preview" }).click();
  await expect(surface.getByRole("button", { name: "添加选区到对话", exact: true })).toBeVisible();
  await surface.getByRole("button", { name: "查看预览", exact: true }).click();
  // 铺满：面板占满对话区、主区隐藏；还原后回到停靠宽度。
  await surface.getByRole("button", { name: "铺满文件面板", exact: true }).click();
  await expect(page.getByRole("main")).toBeHidden();
  await surface.getByRole("button", { name: "还原文件面板", exact: true }).click();
  await expect(page.getByRole("main")).toBeVisible();
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
  // 行内「+」只在悬停时出现，先悬停工作区行。
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByText("README.md", { exact: true }).click();
  await expect(
    surface.getByRole("heading", { name: "Workspace preview" }),
  ).toBeVisible();
  await expect(surface.getByRole("textbox", { name: "搜索工作区文件名" })).toHaveCount(0);
  await surface.getByRole("tab", { name: "README.md", exact: true }).dblclick();
  await surface.getByRole("button", { name: "查看源码", exact: true }).click();
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
  await expect(surface.getByRole("button", { name: "查看预览", exact: true })).toBeVisible();
  await surface.getByRole("button", { name: "更多阅读选项", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "自动换行", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.keyboard.press("Escape");
  await markdownTab.press("End");
  await expect(codeTab).toHaveAttribute("aria-selected", "true");
  await expect(codeTab).toBeFocused();
  const findRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/files/find-text")) findRequests.push(request.url());
  });
  await page.keyboard.press("ControlOrMeta+f");
  await expect(surface.getByLabel("在文件中查找")).toBeFocused();
  // 整份加载的小文件就地查找：边输入边高亮，不请求后端，也没有「整个文件查找」入口。
  await surface.getByLabel("在文件中查找").fill("greet");
  await expect(surface.getByText("1/1", { exact: true })).toBeVisible();
  await expect(surface.locator(".cm-findMatch-current")).toHaveText("greet");
  await expect(surface.getByRole("button", { name: "在整个文件中查找", exact: true })).toHaveCount(0);
  expect(findRequests).toEqual([]);
  await surface.getByRole("button", { name: "关闭搜索", exact: true }).click();
  await expect(surface.locator(".cm-findMatch")).toHaveCount(0);
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
  await surface.getByRole("button", { name: "查看预览", exact: true }).click();
  await surface.getByRole("heading", { name: "Workspace preview" }).click();
  await page.keyboard.press("ControlOrMeta+f");
  await expect(surface.getByLabel("在文件中查找")).toBeFocused();
  // 渲染视图就地高亮（CSS Custom Highlight），不再切回源码。
  await surface.getByLabel("在文件中查找").fill("workspace");
  await expect(surface.getByText("1/1", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => CSS.highlights.has("cosir-find-current"))).toBe(true);
  await expect(surface.getByRole("heading", { name: "Workspace preview" })).toBeVisible();
  await surface
    .getByRole("button", { name: "关闭文件 README.md", exact: true })
    .click();
  await expect(surface.getByRole("textbox", { name: "搜索工作区文件名" })).toBeFocused();
});

for (const theme of ["light", "dark"]) {
  test(`${theme} 代码对比度、宽面板与缩放布局`, async ({ page }) => {
    await page.addInitScript(
      (value) => localStorage.setItem("cosir:theme", JSON.stringify({ state: { preference: value }, version: 0 })),
      theme,
    );
    await mockFiles(page);
    await page.goto("/");
    // 行内「+」只在悬停时出现，先悬停工作区行。
    await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
    await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
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
    await page.setViewportSize({ width: 1620, height: 900 });
    const outer = page.getByRole("separator", { name: "调整文件宽度" });
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

test("搜索停止重试、未扫描原因与编码变更重跑查找", async ({ page }) => {
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
  // 行内「+」只在悬停时出现，先悬停工作区行。
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
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
  // 整文件查找只服务分段读取的大文件：把 main.py 伪装成 3MB、只读到第一段。
  const largeSize = 3 * 1024 * 1024;
  await page.route("**/workspaces/*/files/**", async (route) => {
    const url = new URL(route.request().url());
    const operation = url.pathname.split("/").at(-1);
    if (url.searchParams.get("path") !== "main.py") return route.fallback();
    if (operation === "info")
      return route.fulfill({ json: { path: "main.py", name: "main.py", root_path: "/workspace/demo", size: largeSize, modified_ns: 1_700_000_000_000_000_000, version: "v1", kind: "text", mime: "text/plain", encoding: "utf-8" } });
    if (operation === "text")
      return route.fulfill({ json: { text: source, offset: 0, next_offset: source.length, size: largeSize, version: "v1", encoding: "utf-8" } });
    return route.fallback();
  });
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
  const findEncodings: string[] = [];
  await page.route("**/files/find-text", async (route) => {
    const encoding = String(route.request().postDataJSON().encoding);
    findEncodings.push(encoding);
    if (encoding === "cp1252") {
      await route.fulfill({
        contentType: "application/x-ndjson",
        body:
          JSON.stringify({
            matches: [{ offset: 0, snippet: "新编码结果" }],
            complete: true,
          }) + "\n",
      });
      return;
    }
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
  // 先就地查当前分段，再按需查整个文件。
  await surface.getByLabel("在文件中查找").fill("greet");
  await expect(surface.getByText("1/1", { exact: true })).toBeVisible();
  await surface.getByRole("button", { name: "在整个文件中查找", exact: true }).click();
  await expect.poll(() => findStarted).toBe(true);
  await surface.getByRole("button", { name: "更多阅读选项", exact: true }).click();
  await page.getByLabel("文本编码").selectOption("cp1252");
  await page.keyboard.press("Escape");
  // 换编码后查找按新编码重跑：旧请求被中止，结果来自新编码。
  await expect.poll(() => findEncodings.at(-1)).toBe("cp1252");
  await expect(surface.getByText("新编码结果", { exact: true })).toBeVisible();
  await expect(surface.getByText(/查找完成/)).toBeVisible();
  releaseFind();
  await expect.poll(() => findDelivered).toBe(true);
  await expect(
    surface.getByText("旧编码迟到结果", { exact: true }),
  ).toHaveCount(0);
});

test("阅读工具栏在窄宽度保持单行，菜单与搜索焦点独立", async ({ page }, testInfo) => {
  await mockFiles(page);
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  // 行内「+」只在悬停时出现，先悬停工作区行。
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByRole("treeitem", { name: "README.md", exact: true }).click();
  for (const width of [320, 480, 650]) {
    await page.evaluate(async width => {
      const modulePath = "/lib/layout/preferences.ts";
      const { useLayoutPreferences } = await import(modulePath);
      useLayoutPreferences.getState().setDockWidth("files", width);
    }, width);
    const toolbar = surface.getByLabel("文件阅读工具栏");
    await expect(toolbar).toBeVisible();
    await expect.poll(() => toolbar.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
    const bounds = await toolbar.boundingBox();
    expect(bounds!.height).toBeLessThan(46);
    const body = await surface.getByRole("heading", { name: "Workspace preview" }).boundingBox();
    const panel = await page.getByRole("complementary", { name: "文件" }).boundingBox();
    expect(body!.y - panel!.y).toBeLessThan(160);
    await page.screenshot({ path: testInfo.outputPath(`reading-${width}.png`) });
  }
  await surface.getByRole("button", { name: "查找文件内容", exact: true }).click();
  await surface.getByRole("button", { name: "更多阅读选项", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("文本编码")).toBeHidden();
  await expect(surface.getByLabel("在文件中查找")).toBeVisible();
  await surface.getByLabel("在文件中查找").focus();
  await page.keyboard.press("Escape");
  await expect(surface.getByRole("button", { name: "查找文件内容", exact: true })).toBeFocused();
  await surface.getByRole("button", { name: "查看源码", exact: true }).click();
  await surface.getByRole("button", { name: "查找文件内容", exact: true }).click();
  await surface.locator(".cm-content").click();
  await page.keyboard.press("Escape");
  await expect(surface.getByLabel("在文件中查找")).toBeHidden();
  await page.evaluate(async () => {
    const modulePath = "/lib/layout/preferences.ts";
    (await import(modulePath)).useLayoutPreferences.getState().setDockWidth("files", 480);
  });
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await expect(surface.getByRole("textbox", { name: "搜索工作区文件名" })).toBeFocused();
});

test("同名标签消歧、溢出菜单、临时替换和关闭后的目录入口", async ({ page }) => {
  await mockFiles(page);
  await page.goto("/");
  // 行内「+」只在悬停时出现，先悬停工作区行。
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await page.evaluate(async () => {
    const modulePath = "/lib/workspace-files/workspace-files-store.ts";
    const store = (await import(modulePath)).useFilesStore.getState();
    for (const path of ["src/index.ts", "test/index.ts", ...Array.from({length: 12}, (_, i) => `deep/folder/file-${i}.ts`)]) store.open(7, path, true);
    store.open(7, "temporary.ts");
    store.open(7, "replacement.ts");
  });
  await expect(surface.getByRole("tab", { name: "temporary.ts", exact: true })).toHaveCount(0);
  await expect(surface.getByRole("tab", { name: "replacement.ts", exact: true })).toHaveAttribute("aria-selected", "true");
  // 标签条放不下时才出现「全部文件」。
  await surface.getByRole("button", { name: "全部文件", exact: true }).click();
  await page.getByRole("menuitemradio", { name: "test/index.ts", exact: true }).click();
  await expect(surface.getByRole("tab", { name: "test/index.ts", exact: true })).toHaveAttribute("aria-selected", "true");
  const active = surface.getByRole("tab", { name: "test/index.ts", exact: true });
  const list = surface.getByRole("tablist", { name: "文件预览标签" });
  expect((await active.boundingBox())!.x).toBeGreaterThanOrEqual((await list.boundingBox())!.x);
  await expect(surface.getByRole("tab", { name: "src/index.ts", exact: true })).toHaveCount(1);
  await page.evaluate(async () => {
    const modulePath = "/lib/workspace-files/workspace-files-store.ts";
    const store = (await import(modulePath)).useFilesStore.getState();
    store.patch(7, { treeVisible: false });
    for (const tab of store.byWorkspace[7].tabs) store.close(7, tab.path);
  });
  await expect(surface.getByRole("textbox", { name: "搜索工作区文件名" })).toBeVisible();
});

test("文件菜单开关不锁定主对话，长消息滚动位置和节点保持稳定", async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await mockFiles(page);
  await request.post(`${API_ORIGIN}/__test__/seed-tool-trace`);
  const state = await readState(request, 42);
  state.messages[1].parts = Array.from({ length: 45 }, (_, i) => [
    { type: "text" as const, text: `分析第 ${i} 次输出`, status: "completed" as const },
    { type: "text" as const, text: `第 ${i} 次读取终端输出。继续观察命令的执行过程，检查输出和状态是否符合预期。`, status: "completed" as const },
  ]).flat();
  await seedState(request, 42, state);
  await page.goto("/sessions/42");
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const trigger = page.getByRole("button", { name: "文件浏览器更多选项" });
  const viewport = page.locator(".aui-thread-root > div");
  const row = page.locator('main [data-index="1"]');
  await expect(row).toContainText("第 44 次读取终端输出");
  // 没有最终回复时过程保持展开，这就是本用例要验证的长消息滚动画布。
  await viewport.evaluate((element) => { element.scrollTop = 1200; });
  await expect.poll(() => viewport.evaluate((element) => element.scrollTop)).toBe(1200);
  const originalRow = await row.elementHandle();
  const originalBounds = await viewport.boundingBox();

  for (let cycle = 0; cycle < 3; cycle++) {
    await trigger.click();
    await expect(page.getByRole("menuitem", { name: "管理忽略项" })).toBeVisible();
    // 普通面板菜单不能把整个文件面板变成不可访问、不可点击的模态背景。
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

test("对话正文的文件链接在文件面板打开并定位到行，外部链接先确认", async ({ page, request }) => {
  await mockFiles(page);
  await request.post(`${API_ORIGIN}/__test__/seed-tool-trace`);
  const state = await readState(request, 42);
  state.messages[1].parts = [{
    type: "text" as const,
    text: "见 [greet 定义](main.py#L2-L3)，参考 [文档](https://example.com/docs)，危险 [链接](javascript:alert(1))。",
    status: "completed" as const,
  }];
  await seedState(request, 42, state);
  await page.goto("/sessions/42");

  const message = page.locator('main [data-index="1"]');
  await expect(message.getByText("链接", { exact: true })).not.toHaveAttribute("href");
  await message.getByRole("link", { name: "greet 定义" }).click();
  const editor = page.getByRole("textbox", { name: "只读文件 main.py" });
  await expect(editor).toBeVisible();
  // 第 2–3 行被选中：Ctrl/Cmd+L 把选区添加到对话，胶囊带着这个行范围。
  await expect(page.getByRole("button", { name: "添加选区到对话", exact: true })).toBeVisible();
  await editor.focus();
  await page.keyboard.press("ControlOrMeta+l");
  await expect(page.getByRole("textbox", { name: "消息输入" })).toContainText("L2–3");

  await message.getByRole("link", { name: "文档" }).click();
  const dialog = page.getByRole("dialog", { name: "打开外部链接？" });
  await expect(dialog).toContainText("https://example.com/docs");
  await dialog.getByRole("button", { name: "取消" }).click();
  await expect(dialog).toHaveCount(0);
});

test("链接指向不存在的文件：不留坏标签，提到的文件名改为查找，已打开的文件被删后显示中性状态", async ({ page, request }) => {
  await mockFiles(page);
  // session.ts 与 src/gone.ts 在工作区里不存在；main.py 先能读，之后模拟被删除。
  let mainDeleted = false;
  await page.route("**/workspaces/*/files/info?*", async (route) => {
    const path = new URL(route.request().url()).searchParams.get("path");
    if (path === "session.ts" || path === "src/gone.ts" || (path === "main.py" && mainDeleted))
      return route.fulfill({ status: 404, json: { detail: { code: "FILE_NOT_FOUND", message: "文件不存在" } } });
    return route.fallback();
  });
  await request.post(`${API_ORIGIN}/__test__/seed-tool-trace`);
  const state = await readState(request, 42);
  state.messages[1].parts = [{
    type: "text" as const,
    text: "会话核心在 `session.ts`，入口见 [main](main.py)，旧文件 [gone](src/gone.ts)。",
    status: "completed" as const,
  }];
  await seedState(request, 42, state);
  await page.goto("/sessions/42");
  const message = page.locator('main [data-index="1"]');
  const surface = page.getByTestId("workspace-files-surface");

  // 裸文件名只提供显式查找入口，不探测根目录。
  await message.getByRole("button", { name: "查找文件 session.ts" }).click();
  await expect(surface.getByRole("textbox", { name: "搜索工作区文件名" })).toHaveValue("session.ts");
  await expect(surface.getByRole("tab", { name: "session.ts" })).toHaveCount(0);

  // 明确的地址不存在：提示并提供查找，不开标签。
  await message.getByRole("link", { name: "gone" }).click();
  await expect(page.getByText("工作区里没有 src/gone.ts")).toBeVisible();
  await expect(surface.getByRole("tab", { name: "gone.ts" })).toHaveCount(0);

  // 已打开的文件被删除：中性的「文件不存在」，提供查找与关闭，不提供「重新读取」。
  await message.getByRole("link", { name: "main" }).click();
  await expect(surface.getByRole("tab", { name: "main.py", exact: true })).toHaveAttribute("aria-selected", "true");
  mainDeleted = true;
  // 刷新等同文件监听报告变化：重新读取文件信息后得到「不存在」。
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await surface.getByRole("button", { name: "刷新工作区文件", exact: true }).click();
  await surface.getByRole("button", { name: "切换目录面板", exact: true }).click();
  await expect(surface.getByText("文件不存在", { exact: true })).toBeVisible();
  await expect(surface.getByRole("button", { name: "查找同名文件" })).toBeVisible();
  await expect(surface.getByRole("button", { name: "重新读取" })).toHaveCount(0);
  await surface.getByRole("button", { name: "关闭标签" }).click();
  await expect(surface.getByRole("tab", { name: "main.py", exact: true })).toHaveCount(0);
});
