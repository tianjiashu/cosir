import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { activity, assistantMessage, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";

/** 渲染链接与图片不联网：站点只用内置品牌图标，网站图片换成链接（宿主另拦下界面的一切公网请求）。 */
const REPLY = [
  "[owner/repo#1](https://github.com/owner/repo/pull/1)",
  "[没有内置图标的站点](https://missing.example/a)",
  "![架构图](https://img.example/chart.png?d=1)",
  "[本机服务](http://localhost:3000)",
  "[配置](src/app.ts#L4)",
  "[目录](src/)",
  "[截图](assets/shot.png)",
  "[脚注](#note)",
  "`src/inline.ts`",
].join(" · ");

const search = toolCallPart("search", "web_search", {
  status: "completed",
 
  args: { query: "icons" },
  display_data: { kind: "web-search-results", results: [{ url: "https://icon.example/result", title: "搜索结果" }] },
});

test.beforeEach(async ({ request }) => {
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
});

for (const colorScheme of ["light", "dark"] as const) test(`conversation objects carry their icons in ${colorScheme} mode`, async ({ page }) => {
  await page.emulateMedia({ colorScheme });
  await page.goto("/sessions/42");
  const link = (name: string) => page.getByRole("link", { name, exact: true });

  await expect(link("owner/repo#1").locator("svg path")).toHaveCount(1);
  await expect(link("没有内置图标的站点").locator("svg.lucide-globe")).toBeVisible();
  // 网站图片是一条带站点图标的链接，不是 <img>：一显示就请求会把路径和参数送出去。
  await expect(link("图片：架构图")).toHaveAttribute("href", "https://img.example/chart.png?d=1");
  await expect(page.locator('[data-role="assistant"] img, a img')).toHaveCount(0);
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
  await expect(result.locator("xpath=..").locator("svg.lucide-globe")).toBeVisible();
  await expect(result.locator("xpath=..").locator("img")).toHaveCount(0);

  await result.click();
  await expect(page.getByText("打开外部链接？")).toBeVisible();
});
