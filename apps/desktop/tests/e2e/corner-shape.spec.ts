import { expect, test } from "@playwright/test";

import { API_ORIGIN } from "./e2e-env";
import { completedState, seedState } from "./transport-fixtures";

/** 验证真实组件及编译后的 CSS：圆形覆盖、important 变体、局部圆角和第三方入口不能漏掉曲线契约。 */
for (const theme of ["light", "dark"] as const) {
  test(`${theme}：矩形与圆形各自保持轮廓，弹层局部圆角和 Toast 同步`, async ({ page, request }, testInfo) => {
    await request.post(`${API_ORIGIN}/__test__/reset`);
    await request.post(`${API_ORIGIN}/__test__/seed-session`, { data: { sessionId: 100, title: "圆角验收" } });
    await seedState(request, 100, completedState(1, { id: "u", parts: [{ type: "text", text: "检查代码与表格" }] }, [
      { type: "text", text: "```python\nprint(1)\n```\n\n| 项目 | 状态 |\n| --- | --- |\n| 圆角 | 完成 |", status: "completed" },
    ]));
    await page.addInitScript(preference => {
      localStorage.clear();
      localStorage.setItem("cosir:theme", JSON.stringify({ state: { preference }, version: 0 }));
    }, theme);
    await page.goto("/sessions/100");
    const input = page.getByLabel("消息输入");
    await input.fill("准备发送");
    await expect(page.locator("form").filter({ has: input })).toHaveCSS("corner-shape", "superellipse(1.5)");
    await expect(page.getByRole("button", { name: "发送", exact: true })).toHaveCSS("corner-shape", /^(round|superellipse\(1\))$/);
    await expect(page.getByRole("button", { name: "新会话", exact: true })).toHaveCSS("corner-shape", "superellipse(1.5)");
    await expect(page.locator('[data-streamdown="code-block"]')).toHaveCSS("corner-shape", "superellipse(1.5)");
    await expect(page.locator('[data-streamdown="table-wrapper"] > div:last-child')).toHaveCSS("corner-shape", "superellipse(1.5)");

    // 通过现有应用入口打开真实确认弹窗，不新增生产测试接口。
    await page.evaluate(async modulePath => {
      const { requestConfirmation } = await import(/* @vite-ignore */ modulePath);
      void requestConfirmation({ title: "确认圆角", description: "检查底栏与外框的拼接", confirmLabel: "确认" });
    }, "/lib/confirm/confirm-store.ts");
    const dialog = page.getByRole("dialog", { name: "确认圆角" });
    await expect(dialog).toHaveCSS("corner-shape", "superellipse(1.5)");
    const footer = dialog.locator('[data-slot="dialog-footer"]');
    await expect(footer).toHaveCSS("corner-bottom-left-shape", "superellipse(1.5)");
    await expect(footer).toHaveCSS("corner-top-left-shape", /^(round|superellipse\(1\))$/);
    await expect(footer).toHaveCSS("border-bottom-left-radius", "14px");
    await expect(dialog.getByRole("button", { name: "取消" })).toBeFocused();
    await dialog.screenshot({ path: testInfo.outputPath(`dialog-corners-${theme}.png`), animations: "disabled" });
    await page.keyboard.press("Escape");

    await page.getByRole("button", { name: "搜索会话", exact: true }).click();
    await expect(page.locator('[data-slot="command"]')).toHaveCSS("corner-shape", "superellipse(1.5)");
    await expect(page.locator('[data-slot="command-input-wrapper"] [data-slot="input-group"]')).toHaveCSS("corner-shape", "superellipse(1.5)");
    await page.keyboard.press("Escape");

    await page.evaluate(async modulePath => {
      const { notify } = await import(/* @vite-ignore */ modulePath);
      notify.info("圆角通知", { action: { label: "检查", onClick: () => {} } });
    }, "/lib/notify.ts");
    const toast = page.locator("[data-sonner-toast]").filter({ hasText: "圆角通知" });
    await expect(toast).toHaveAttribute("data-mounted", "true");
    await expect(toast).toHaveCSS("corner-shape", "superellipse(1.5)");
    await expect(toast.locator("[data-button]")).toHaveCSS("corner-shape", "superellipse(1.5)");
    await expect(toast.locator("[data-close-button]")).toHaveCSS("corner-shape", /^(round|superellipse\(1\))$/);
    await toast.screenshot({ path: testInfo.outputPath(`toast-corners-${theme}.png`), animations: "disabled" });
  });
}
