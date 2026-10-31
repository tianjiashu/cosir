import fs from "node:fs";

const POLL_INTERVAL_MS = 250;
const PROBE_TIMEOUT_MS = 500;

/**
 * 等待后端就绪：后端先写启动状态文件（bootstate），HTTP 服务起来后 `/health` 返回 200。
 *
 * 失败的判据有三个，任一成立即返回错误描述：进程提前退出、bootstate 报告 `failed`（带后端给出的结构化
 * 原因，比超时更早、更准确）、超时。`isCurrent` 为 false 表示这次启动已被新的生命周期操作取代，
 * 立即以 `superseded` 返回，调用方不再发布任何状态。`/health` 只表示存活，不代表数据库与模型都可用。
 */
export async function waitForBackend(options: {
  baseUrl: string;
  bootstateFile: string;
  timeoutMs: number;
  isAlive: () => boolean;
  isCurrent: () => boolean;
}): Promise<{ ok: true } | { ok: false; superseded: boolean; message: string }> {
  const deadline = Date.now() + options.timeoutMs;
  while (Date.now() < deadline) {
    if (!options.isCurrent()) return { ok: false, superseded: true, message: "后端启动已被新的生命周期操作取消" };
    if (!options.isAlive()) return { ok: false, superseded: false, message: "本地 Agent 后端进程在健康检查前退出" };
    const failure = readBootstateFailure(options.bootstateFile);
    if (failure) return { ok: false, superseded: false, message: failure };
    if (await probeHealth(options.baseUrl)) return { ok: true };
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
  return { ok: false, superseded: false, message: `后端在 ${Math.round(options.timeoutMs / 1000)} 秒内未通过健康检查` };
}

export function readBootstateFailure(file: string): string | null {
  let state: unknown;
  try {
    state = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
  if (typeof state !== "object" || state === null || (state as { phase?: unknown }).phase !== "failed") return null;
  const { error_message: message, error_type: errorType } = state as { error_message?: unknown; error_type?: unknown };
  return `本地 Agent 后端启动失败（${typeof errorType === "string" ? errorType : "启动错误"}）：${typeof message === "string" ? message : "后端启动失败"}`;
}

async function probeHealth(baseUrl: string): Promise<boolean> {
  try {
    const response = await fetch(`${baseUrl}/health`, { signal: AbortSignal.timeout(PROBE_TIMEOUT_MS) });
    return response.ok;
  } catch {
    return false;
  }
}
