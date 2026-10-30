import type { APIRequestContext } from "@playwright/test";

import type { TransportMessagePart, TransportState } from "@/lib/assistant/contract";

/** E2E 共用的 Task 视角 snapshot 构造器，与 tests/e2e/test-server.mjs 的契约一致。 */
const LAUNCH = { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] };

export function completedState(
  runId: number,
  user: { id: string; parts: TransportMessagePart[] },
  assistantParts: TransportMessagePart[],
): TransportState {
  const outcome = { status: "completed" as const, endReason: "stop", error: null, usage: null, contextWindowTotal: null };
  return {
    title: "测试对话",
    messages: [
      { id: user.id, role: "user", parts: user.parts, runId, outcome: null },
      { id: `assistant-${user.id}`, role: "assistant", parts: assistantParts, runId, outcome },
    ],
    messageOffset: 0, activity: { runId, ...outcome, launch: LAUNCH },
    terminal_sessions: [], background_tasks: [], foreground_waits: [],
  };
}

/** 让模拟后端持有这份 state：`/assistant/state` 与 attach 首帧读到的是同一份事实。 */
/** 种子 state 可省略进程内事实（终端会话、看板、前台等待）与 `messageOffset`：模拟后端注入它们，并按历史窗口裁剪。 */
type ProcessFacts = "terminal_sessions" | "background_tasks" | "foreground_waits" | "messageOffset";
type SeedState = Omit<TransportState, ProcessFacts> & Partial<Pick<TransportState, ProcessFacts>>;

export async function seedState(request: APIRequestContext, taskId: number, state: SeedState): Promise<void> {
  await request.post("http://127.0.0.1:8000/__test__/seed-state", { data: { taskId, state } });
}
