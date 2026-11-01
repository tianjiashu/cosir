import { getBrowserTabContext } from "@/lib/api/browser";
import { desktopHost } from "@/lib/desktop-host";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useBrowserPanelStore } from "./panel-store";

/**
 * 用户在浏览器面板里新开标签（可直接打开地址）。标签归这个 Task，Agent 经 `browser_tabs` 看得到也能操作，
 * 与交互终端「用户和 Agent 都能新建会话」一致。
 *
 * 上限与下载目录的事实源都在后端（配置中心、Task 所在工作区）：宿主不存配置，这里每次现读
 * `GET /tasks/{id}/browser-context` 随请求带给宿主，与 Agent 开标签受同样约束。读取失败、超出上限或地址
 * 不合法时抛出带原因的错误，由调用方提示。
 */
export async function openUserTab(taskId: number, url?: string): Promise<string> {
  const host = desktopHost!;
  const context = await getBrowserTabContext(taskId);
  const tabId = await host.browser.open(taskId, context, url);
  useBrowserPanelStore.getState().focus(taskId, tabId);
  void frontendLog("INFO", "browser_tab_opened_by_user", "用户在浏览器面板新开标签", { data: { taskId, tabId } });
  return tabId;
}
