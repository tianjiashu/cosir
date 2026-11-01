import type { Page } from "@playwright/test";

/** 开发模式下 `frontendLog` 同时写进 `window.__cosirFrontendLogs`，E2E 据此读前端诊断事件。 */
export type FrontendLog = {
  event?: string;
  data?: Record<string, unknown>;
};

/** 当前页面加载以来的前端日志（页面重载后从零开始）。 */
export async function frontendLogs(page: Page): Promise<FrontendLog[]> {
  return page.evaluate(() => {
    const value = (window as unknown as { __cosirFrontendLogs?: FrontendLog[] }).__cosirFrontendLogs;
    return value ?? [];
  });
}

/**
 * 当前页面加载以来已建立的长订阅条数，按「订阅已建立」日志计（主对话为
 * `assistant_transport_subscription_opened`，Workbench 阅读页为 `workbench_agent_subscription_opened`）。
 *
 * 订阅一律这样数，不数 attach 网络请求或响应：开发模式 StrictMode 挂载时会多发一次随即被清理中止的
 * attach，它有时已拿到响应头；被替换的连接不回调 `onResponse`，只有真正建立的订阅才记这条日志。
 * 同理，往 attach 注入失败（`fail-next-attach`）也要等这条日志出现、订阅建立之后再做。
 */
export async function openedSubscriptionCount(
  page: Page,
  { event = "assistant_transport_subscription_opened", taskId }: { event?: string; taskId?: number } = {},
): Promise<number> {
  return (await frontendLogs(page)).filter((entry) => (
    entry.event === event && (taskId === undefined || entry.data?.taskId === taskId)
  )).length;
}
