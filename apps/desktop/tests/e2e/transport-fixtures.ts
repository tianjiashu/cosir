import type { APIRequestContext } from "@playwright/test";

import type { TransportMessagePart, TransportState } from "@/lib/assistant/contract";
import { API_ORIGIN } from "./e2e-env";

/** E2E 共用的 Session 视角 snapshot 构造器，与 tests/e2e/test-server.mjs 的契约一致。 */
const LAUNCH = { modelConfigId: null, reasoningEffort: null };

/** 一个已完成回合的快照；第一个参数是回合号（回复 id 按它编码）。 */
export function completedState(
  turn: number,
  user: { id: string; parts: TransportMessagePart[] },
  assistantParts: TransportMessagePart[],
): TransportState {
  // `durationMs` 只属于结局；activity 是固定字段的白名单，多一个键整份快照就无效。
  const activityOutcome = { status: "completed" as const, endReason: "stop", error: null, usage: null, contextWindowTotal: null };
  const outcome = { ...activityOutcome, durationMs: 0 };
  return {
    title: "测试对话",
    messages: [
      { id: user.id, role: "user", parts: user.parts, turn, inherited: false, source: null, outcome: null },
      { id: `assistant-turn-${turn}`, role: "assistant", parts: assistantParts, turn, inherited: false, outcome, source: null },
    ],
    messageOffset: 0, activity: { modelActivity: null, turn, resumable: false, ...activityOutcome, launch: LAUNCH, startedAt: "2026-10-27T00:00:00.000Z" },
    terminal_sessions: [], background_tasks: [], foreground_waits: [], handoffs: [],
  };
}

/** 种子 state 可省略进程内事实（终端会话、看板、前台等待）与 `messageOffset`：模拟后端注入它们，并按历史窗口裁剪。 */
type ProcessFacts = "terminal_sessions" | "background_tasks" | "foreground_waits" | "handoffs" | "messageOffset";
type SeedState = Omit<TransportState, ProcessFacts> & Partial<Pick<TransportState, ProcessFacts>>;

/** 让模拟后端持有这份 state：attach 首帧与 `readState` 读到的是同一份事实。 */
export async function seedState(request: APIRequestContext, sessionId: number, state: SeedState): Promise<void> {
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId, state } });
}

/** 模拟后端此刻的 canonical state（attach 首帧会带的那份），供断言后端事实。 */
export async function readState(request: APIRequestContext, sessionId: number): Promise<TransportState> {
  return await (await request.get(`${API_ORIGIN}/__test__/state?sessionId=${sessionId}`)).json() as TransportState;
}
