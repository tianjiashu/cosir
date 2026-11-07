import { forgetDisclosureHeight } from "./message-height-cache";
import { create } from "zustand";

/**
 * 回合内各区的开合状态，按 Task 分桶、不持久化。
 *
 * 为什么不放组件局部 state：虚拟列表卸载再挂载会丢开合。这里只记用户覆盖（true / false），没有覆盖时
 * 由组件按规则给默认值（过程区有最终回复且正常完成时收起；普通工具组和时间线终端详情默认收起），所以阶段变化不会被旧覆盖卡住——
 * 覆盖只在用户点过之后才存在。
 *
 * 键：`process:{messageId}`（过程区）、`call:{toolCallId}`（调用行）、
 * `activity:{messageId}:{intervalKey}`（工具区间）。Task 删除时 `clearTask` 整桶丢掉。
 */
export type DisclosureKey = `terminal-reader:${string}` | `terminal-command:${string}` | `process:${string}` | `call:${string}` | `activity:${string}:${string}` | `agent:${string}` | `file:${string}:${string}`;

export const activityKey = (messageId: string, intervalKey: string): DisclosureKey => `activity:${messageId}:${intervalKey}`;
export const agentKey = (messageId: string): DisclosureKey => `agent:${messageId}`;
export const fileKey = (messageId: string, path: string): DisclosureKey => `file:${messageId}:${path}`;

export const processKey = (messageId: string): DisclosureKey => `process:${messageId}`;
export const callKey = (toolCallId: string): DisclosureKey => `call:${toolCallId}`;

type Overrides = Readonly<Record<string, boolean>>;

type DisclosureState = {
  /** taskId → 该 Task 内的用户覆盖。 */
  tasks: Readonly<Record<number, Overrides>>;
  terminalPositions: Readonly<Record<number, Readonly<Record<string, number>>>>;
  setTerminalPosition: (taskId: number, callId: string, line: number) => void;
  set: (taskId: number, key: DisclosureKey, open: boolean) => void;
  /** 清掉某个键的覆盖，之后回到默认规则。 */
  reset: (taskId: number, key: DisclosureKey) => void;
  clearTask: (taskId: number) => void;
};

export const useDisclosureStore = create<DisclosureState>()((set) => ({
  tasks: {},
  terminalPositions: {},
  setTerminalPosition: (taskId, callId, line) => set((state) => ({ terminalPositions: {
    ...state.terminalPositions, [taskId]: { ...state.terminalPositions[taskId], [callId]: line },
  } })),
  set: (taskId, key, open) => {
    forgetDisclosureHeight(taskId, key);
    set((state) => ({
    tasks: { ...state.tasks, [taskId]: { ...state.tasks[taskId], [key]: open } },
  }));
  },
  reset: (taskId, key) => set((state) => {
    forgetDisclosureHeight(taskId, key);
    const bucket = state.tasks[taskId];
    if (!bucket || !(key in bucket)) return state;
    return { tasks: { ...state.tasks, [taskId]: without(bucket, key) } };
  }),
  clearTask: (taskId) => set((state) => !(taskId in state.tasks) && !(taskId in state.terminalPositions) ? state : ({ tasks: without(state.tasks, taskId), terminalPositions: without(state.terminalPositions, taskId) })),
}));

function without<T>(record: Readonly<Record<PropertyKey, T>>, key: PropertyKey): Record<PropertyKey, T> {
  const copy: Record<PropertyKey, T> = { ...record };
  delete copy[key];
  return copy;
}

/** 某个键的开合：有用户覆盖按覆盖，否则按调用方给的默认值。非 React 场景用。 */
export function resolveDisclosure(taskId: number, key: DisclosureKey, defaultOpen: boolean): boolean {
  return useDisclosureStore.getState().tasks[taskId]?.[key] ?? defaultOpen;
}

/** React 订阅版：只订阅这一个键，默认值变化（如阶段从运行中到结束）时无覆盖的键随之变化。 */
export function useDisclosure(taskId: number, key: DisclosureKey, defaultOpen: boolean): [boolean, (open: boolean) => void] {
  const open = useDisclosureStore((state) => state.tasks[taskId]?.[key] ?? defaultOpen);
  const setOpen = useDisclosureStore((state) => state.set);
  return [open, (next) => setOpen(taskId, key, next)];
}
