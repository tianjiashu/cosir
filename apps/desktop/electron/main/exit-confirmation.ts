import { dialog } from "electron";

import { logDesktopEvent } from "./desktop-log";
import { getMainWindow } from "./window";

const ACTIVE_COUNT_PATH = "/terminal/sessions/active-count";
/** 查询只为提醒服务：宁可漏提醒，也不能让退出卡住，所以时限很短。 */
const ACTIVE_COUNT_TIMEOUT_MS = 1_000;
const LOG_CALLER = "exit_confirmation";

/**
 * 退出前确认：还有运行中的终端会话时提醒用户，退出会结束它们。返回 true 表示继续退出。
 *
 * 后端未就绪、查询失败或超时都直接放行，不阻塞用户退出。只查终端会话：它们是用户可能正在用、
 * 退出后无法恢复的进程；Agent Run 会在后端重启后收敛为 cancelled，用户可在对话里继续。
 */
export async function confirmExit(readyBaseUrl: string | null): Promise<boolean> {
  if (!readyBaseUrl) {
    logDesktopEvent("INFO", LOG_CALLER, "application_exit_confirmation_skipped", { reason: "backend_not_ready" });
    return true;
  }
  let count: number;
  try {
    count = await queryActiveCount(readyBaseUrl);
  } catch (error) {
    logDesktopEvent("WARNING", LOG_CALLER, "terminal_active_count_query_failed", {}, error);
    return true;
  }
  if (count === 0) return true;
  logDesktopEvent("INFO", LOG_CALLER, "application_exit_confirmation_shown", { active_terminal_sessions: count });
  const options: Electron.MessageBoxOptions = {
    type: "warning",
    title: "退出 Cosir",
    message: `还有 ${count} 个终端会话在运行，退出将结束它们。确定退出吗？`,
    buttons: ["退出", "取消"],
    defaultId: 0,
    cancelId: 1,
  };
  const owner = getMainWindow();
  const { response } = owner ? await dialog.showMessageBox(owner, options) : await dialog.showMessageBox(options);
  const confirmed = response === 0;
  logDesktopEvent("INFO", LOG_CALLER, confirmed ? "application_exit_confirmed" : "application_exit_cancelled");
  return confirmed;
}

async function queryActiveCount(baseUrl: string): Promise<number> {
  const response = await fetch(`${baseUrl}${ACTIVE_COUNT_PATH}`, { signal: AbortSignal.timeout(ACTIVE_COUNT_TIMEOUT_MS) });
  if (!response.ok) throw new Error(`终端会话数查询失败：HTTP ${response.status}`);
  const body: unknown = await response.json();
  const count = (body as { count?: unknown }).count;
  if (typeof count !== "number" || !Number.isInteger(count) || count < 0) throw new Error("响应缺少非负整数 count");
  return count;
}
