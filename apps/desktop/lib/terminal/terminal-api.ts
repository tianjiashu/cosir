import { requestJson, postJson, type RequestOptions } from "@/lib/http/client";

/** 当前宿主可用的 shell（后端探测，人与 Agent 看到同一份清单）。 */
export type TerminalShell = {
  kind: string;
  label: string;
  isDefault: boolean;
  /** 无集成的 shell 不支持命令返回等待、命令输出区间、退出码与 cwd 跟踪，UI 标「功能受限」。 */
  integrationSupported: boolean;
};

export function listTerminalShells(options?: RequestOptions) {
  return requestJson<{ shells: TerminalShell[] }>("/terminal/shells", options);
}

/**
 * 用户新建终端会话。返回后会话还要经 Task 长订阅的 snapshot 出现在 `terminal_sessions`；
 * 调用方只用返回的 sessionId 选中标签，不据此推断会话存活。
 */
export function createTerminalSession(taskId: number, shell = "auto") {
  return postJson<{ sessionId: string }>(`/tasks/${taskId}/terminal/sessions`, { shell });
}

/** 结束会话（不可撤销）；结束态同样经 snapshot 投影到达。 */
export function closeTerminalSession(taskId: number, sessionId: string) {
  return requestJson<Record<string, never>>(
    `/tasks/${taskId}/terminal/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
}
