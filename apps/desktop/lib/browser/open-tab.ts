import { getEnvironmentConfiguration } from "@/lib/api/configuration";
import { desktopHost } from "@/lib/desktop-host";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useBrowserPanelStore } from "./panel-store";

const LIMIT_FIELDS = { perTask: "BROWSER_MAX_TABS_PER_TASK", total: "BROWSER_MAX_TABS" } as const;

/**
 * 用户在浏览器面板里新开标签（可直接打开地址）。标签归这个 Task，Agent 经 `browser_tabs` 看得到也能操作，
 * 与交互终端「用户和 Agent 都能新建会话」一致。
 *
 * 上限的事实源是后端配置中心：宿主不存配置，这里每次现读两个上限随请求带给宿主，与 Agent 开标签受同样
 * 约束。超出上限或地址不合法时抛出带中文原因的错误，由调用方提示。
 */
export async function openUserTab(taskId: number, url?: string): Promise<string> {
  const host = desktopHost!;
  const { groups } = await getEnvironmentConfiguration();
  const fields = groups.flatMap((group) => group.fields);
  const limit = (name: string) => {
    const field = fields.find((item) => item.name === name);
    const value = field?.value ?? field?.default;
    return typeof value === "number" ? value : Number(value);
  };
  const tabId = await host.browser.open(taskId, { perTask: limit(LIMIT_FIELDS.perTask), total: limit(LIMIT_FIELDS.total) }, url);
  useBrowserPanelStore.getState().focus(taskId, tabId);
  void frontendLog("INFO", "browser_tab_opened_by_user", "用户在浏览器面板新开标签", { data: { taskId, tabId } });
  return tabId;
}
