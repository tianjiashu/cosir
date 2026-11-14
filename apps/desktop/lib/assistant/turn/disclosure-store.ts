import { forgetDisclosureHeight } from "./message-height-cache";
import { create } from "zustand";

/**
 * 回合内各区的开合状态，按 Session 分桶、不持久化。
 *
 * 为什么不放组件局部 state：虚拟列表卸载再挂载会丢开合。这里只记用户覆盖（true / false），没有覆盖时
 * 由组件按规则给默认值（过程区有最终回复且正常完成时收起；普通工具组和时间线终端详情默认收起），所以阶段变化不会被旧覆盖卡住——
 * 覆盖只在用户点过之后才存在。
 *
 * 键：`process:{messageId}`（过程区）、`call:{toolCallId}`（调用行与后台看板条目：同一次调用的详情）、
 * `activity:{messageId}:{intervalKey}`（工具区间）、`user:{messageId}`（长用户消息的「显示全部」）。Session 删除时
 * `clearSession` 整桶丢掉。
 */
export type DisclosureKey = `terminal-reader:${string}` | `terminal-command:${string}` | `process:${string}` | `call:${string}` | `activity:${string}:${string}` | `agent:${string}` | `file:${string}:${string}` | `user:${string}`;

export const activityKey = (messageId: string, intervalKey: string): DisclosureKey => `activity:${messageId}:${intervalKey}`;
export const agentKey = (messageId: string): DisclosureKey => `agent:${messageId}`;
export const userMessageKey = (messageId: string): DisclosureKey => `user:${messageId}`;
export const fileKey = (messageId: string, path: string): DisclosureKey => `file:${messageId}:${path}`;

export const processKey = (messageId: string): DisclosureKey => `process:${messageId}`;
export const callKey = (toolCallId: string): DisclosureKey => `call:${toolCallId}`;

type Overrides = Readonly<Record<string, boolean>>;

type DisclosureState = {
  /** sessionId → 该 Session 内的用户覆盖。 */
  sessions: Readonly<Record<number, Overrides>>;
  terminalPositions: Readonly<Record<number, Readonly<Record<string, number>>>>;
  setTerminalPosition: (sessionId: number, callId: string, line: number) => void;
  set: (sessionId: number, key: DisclosureKey, open: boolean) => void;
  /** 清掉某个键的覆盖，之后回到默认规则。 */
  reset: (sessionId: number, key: DisclosureKey) => void;
  clearSession: (sessionId: number) => void;
};

export const useDisclosureStore = create<DisclosureState>()((set) => ({
  sessions: {},
  terminalPositions: {},
  setTerminalPosition: (sessionId, callId, line) => set((state) => ({ terminalPositions: {
    ...state.terminalPositions, [sessionId]: { ...state.terminalPositions[sessionId], [callId]: line },
  } })),
  set: (sessionId, key, open) => {
    forgetDisclosureHeight(sessionId, key);
    set((state) => ({
    sessions: { ...state.sessions, [sessionId]: { ...state.sessions[sessionId], [key]: open } },
  }));
  },
  reset: (sessionId, key) => set((state) => {
    forgetDisclosureHeight(sessionId, key);
    const bucket = state.sessions[sessionId];
    if (!bucket || !(key in bucket)) return state;
    return { sessions: { ...state.sessions, [sessionId]: without(bucket, key) } };
  }),
  clearSession: (sessionId) => set((state) => !(sessionId in state.sessions) && !(sessionId in state.terminalPositions) ? state : ({ sessions: without(state.sessions, sessionId), terminalPositions: without(state.terminalPositions, sessionId) })),
}));

function without<T>(record: Readonly<Record<PropertyKey, T>>, key: PropertyKey): Record<PropertyKey, T> {
  const copy: Record<PropertyKey, T> = { ...record };
  delete copy[key];
  return copy;
}

/** 某个键的开合：有用户覆盖按覆盖，否则按调用方给的默认值。非 React 场景用。 */
export function resolveDisclosure(sessionId: number, key: DisclosureKey, defaultOpen: boolean): boolean {
  return useDisclosureStore.getState().sessions[sessionId]?.[key] ?? defaultOpen;
}

/** React 订阅版：只订阅这一个键，默认值变化（如阶段从运行中到结束）时无覆盖的键随之变化。 */
export function useDisclosure(sessionId: number, key: DisclosureKey, defaultOpen: boolean): [boolean, (open: boolean) => void] {
  const open = useDisclosureStore((state) => state.sessions[sessionId]?.[key] ?? defaultOpen);
  const setOpen = useDisclosureStore((state) => state.set);
  return [open, (next) => setOpen(sessionId, key, next)];
}
