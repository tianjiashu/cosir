import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { activity, assistantMessage, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";

const FAVICON = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16'><rect width='16' height='16' fill='%23111'/></svg>";

/** Playwright 拦不到 `/favicon.ico` 请求，所以把取图标用的 `Image` 换成本地结果：`icon.example` 成功，其余失败。 */
function stubFaviconLoading(favicon: string) {
  const NativeImage = window.Image;
  window.Image = class extends NativeImage {
    set src(url: string) {
      super.src = url.startsWith("https://icon.example/") ? favicon : "data:,missing";
    }
  } as typeof Image;
}
const REPLY = [
  "[owner/repo#1](https://github.com/owner/repo/pull/1)",
  "[有图标的站点](https://icon.example/a)",
  "[没有图标的站点](https://missing.example/a)",
  "[本机服务](http://localhost:3000)",
  "[配置](src/app.ts#L4)",
  "[目录](src/)",
  "[截图](assets/shot.png)",
  "[脚注](#note)",
  "`src/inline.ts`",
].join(" · ");

const search = toolCallPart("search", "web_search", {
  status: "completed",
  intent: "查资料",
  args: { query: "icons" },
  display_data: { kind: "web-search-results", results: [{ url: "https://icon.example/result", title: "搜索结果" }] },
});

test.beforeEach(async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-session`, { data: { sessionId: 42, title: "链接图标" } });
  await request.post(`${API_ORIGIN}/__test__/seed-state`, {
    data: {
      sessionId: 42,
      hold: true,
      state: transportState({
        messages: [userMessage("u", 1, "看看 https://github.com/owner/repo"), assistantMessage("a", 1, [search, { type: "text", text: REPLY }])],
        activity: activity(1, "running"),
      }),
    },
  });
  await page.addInitScript(stubFaviconLoading, FAVICON);
});

for (const colorScheme of ["light", "dark"] as const) test(`conversation objects carry their icons in ${colorScheme} mode`, async ({ page }) => {
  await page.emulateMedia({ colorScheme });
  await page.goto("/sessions/42");
  const link = (name: string) => page.getByRole("link", { name, exact: true });

  await expect(link("有图标的站点").locator("img")).toHaveAttribute("src", "https://icon.example/favicon.ico");
  await expect(link("owner/repo#1").locator("svg path")).toHaveCount(1);
  await expect(link("没有图标的站点").locator("svg.lucide-globe")).toBeVisible();
  await expect(link("本机服务").locator("svg.lucide-monitor")).toBeVisible();
  await expect(link("配置").locator("svg.lucide-file")).toBeVisible();
  await expect(link("目录").locator("svg.lucide-folder")).toBeVisible();
  await expect(link("截图").locator("svg.lucide-image")).toBeVisible();
  await expect(page.locator('[data-slot="workspace-code-link"]')).toBeVisible();
  await expect(page.locator('[data-slot="workspace-code-link"] svg')).toHaveCount(0);
  await expect(page.locator('[data-role="user"] a svg')).toBeVisible();
  await page.screenshot({ path: test.info().outputPath(`link-icons-${colorScheme}.png`) });
});

test("tool result links ask before leaving the app", async ({ page }) => {
  await page.goto("/sessions/42");
  await page.locator('[data-slot="activity-head"]').click();
  await page.locator('[data-tool-call-id="search"]').getByRole("button").first().click();
  const result = page.getByRole("link", { name: "搜索结果", exact: true });
  await expect(result).toBeVisible();
  await expect(result.locator("xpath=..").locator("img")).toHaveAttribute("src", "https://icon.example/favicon.ico");

  await result.click();
  await expect(page.getByText("打开外部链接？")).toBeVisible();
});
