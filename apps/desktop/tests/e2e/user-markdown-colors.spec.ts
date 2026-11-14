import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { seedState, completedState } from "./transport-fixtures";

for (const theme of ["light", "dark"]) test(`${theme}: user inline code has readable local colors, including workspace links`, async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-session`, { data: { sessionId: 48, title: "配色" } });
  await seedState(request, 48, completedState(1, { id: "u", parts: [{ type: "text", text: "检查 `inlineToken` 和 `./src/main.ts`。" }] }, [{ type: "text", text: "助手 `inlineToken`", finalReply: true }]));
  await page.addInitScript(theme => localStorage.setItem("cosir:theme", JSON.stringify({ state: { preference: theme }, version: 0 })), theme);
  await page.goto("/sessions/48");
  const codes = page.locator('[data-role="user"] code');
  await expect(codes).toHaveCount(2);
  const contrasts = await codes.evaluateAll(elements => elements.map(code => {
    const canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d")!;
    const pixel = () => [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3);
    const style = getComputedStyle(code);
    ctx.fillStyle = getComputedStyle(code.closest('[data-slot="user-message-bubble"]')!).backgroundColor;
    ctx.fillRect(0, 0, 1, 1);
    const bubble = pixel();
    ctx.fillStyle = style.backgroundColor; ctx.fillRect(0, 0, 1, 1);
    const background = pixel();
    ctx.fillStyle = style.color; ctx.fillRect(0, 0, 1, 1);
    const foreground = pixel();
    const luminance = (rgb: number[]) => rgb.map(value => value / 255).map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
    const a = luminance(background), b = luminance(foreground);
    // 行内代码要在气泡上看得出底色，不能与气泡同色。
    return { contrast: (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05), distinct: background.some((value, index) => value !== bubble[index]) };
  }));
  contrasts.forEach(({ contrast, distinct }) => { expect(contrast).toBeGreaterThan(4.5); expect(distinct).toBe(true); });
  await expect(page.locator('[data-role="user"] [data-slot="workspace-code-link"]')).toHaveCount(1);
});
