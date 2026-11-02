import { getBrowserTabContext } from "@/lib/api/browser";
import { desktopHost } from "@/lib/desktop-host";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { revealBrowserTab } from "./panel-store";

/**
 * 用户在浏览器面板里新开标签（可直接打开地址）。标签归这个 Task，Agent 经 `browser_tabs` 看得到也能操作，
 * 与交互终端「用户和 Agent 都能新建会话」一致。
 *
 * 上限与下载目录的事实源都在后端（配置中心、Task 所在工作区）：宿主不存配置，这里每次现读
 * `GET /tasks/{id}/browser-context` 随请求带给宿主，与 Agent 开标签受同样约束。读取失败或超出上限时抛出，由调用方
 * 提示；开出标签后就切过去，地址打不开（不合法、加载失败）是这个标签上的事，单独提示「页面打开失败」，与在地址栏
 * 导航一致，不算开标签失败。
 */
export async function openUserTab(taskId: number, url?: string): Promise<string> {
  const host = desktopHost!;
  const context = await getBrowserTabContext(taskId);
  const tabId = await host.browser.open(taskId, context);
  revealBrowserTab(taskId, tabId);
  void frontendLog("INFO", "browser_tab_opened_by_user", "用户在浏览器面板新开标签", { data: { taskId, tabId } });
  if (url) {
    host.browser.navigate(tabId, { url }).catch((error: unknown) => {
      notify.error("页面打开失败", { error, fallback: "请检查地址后重试" });
    });
  }
  return tabId;
}
