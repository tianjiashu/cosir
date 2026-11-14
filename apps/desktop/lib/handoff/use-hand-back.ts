import { commandPending, decisionTarget, useCommandRequest } from "@/lib/assistant/command-request-store";
import { useCallback } from "react";

import { submitToolDecision } from "@/lib/assistant/session-commands";
import { HttpError } from "@/lib/http/http-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

/**
 * 「交还」：交接是 HITL 调用，交还即对它作出决定（批准）。面板提示条、工具卡片、关闭交接中的浏览器标签共用
 * 这一个入口；结果随 snapshot 回推。
 */
export async function handBackHandoff(sessionId: number, toolCallId: string): Promise<void> {
  try {
    await submitToolDecision(sessionId, toolCallId, { decision: "approve" });
    void frontendLog("INFO", "handoff_returned", "用户交还交接", { data: { sessionId, toolCallId } });
  } catch (error) {
    // 409：交接已有决定或已不在等待（如已被代为落定），结果随 snapshot 同步，不打扰用户。
    if (!(error instanceof HttpError && error.status === 409)) {
      void frontendLog("WARNING", "handoff_return_failed", "交还交接失败", { data: { sessionId, toolCallId }, error });
      notify.error("交还失败", { error, fallback: "请稍后重试" });
    }
  }
}

/** 带「正在交还」状态的 `handBackHandoff`：按钮据此禁用。 */
export function useHandBack(sessionId: number, toolCallId: string) {
  const request = useCommandRequest(sessionId, decisionTarget(toolCallId));
  const handBack = useCallback((id: string) => handBackHandoff(sessionId, id), [sessionId]);
  return { handBack, returning: commandPending(request) };
}
