import { create } from "zustand";

/**
 * 回合内各区的开合状态，按 Task 分桶、不持久化。
 *
 * 为什么不放组件局部 state：虚拟列表卸载再挂载会丢开合；看板「定位到调用」要在卡片尚未挂载时就把所在
 * 过程区展开。这里只记用户覆盖（true / false），没有覆盖时由组件按规则给默认值（过程区运行中展开、
 * 否则收起；命令行运行中展开……），所以阶段变化不会被旧覆盖卡住——覆盖只在用户点过之后才存在。
 *
 * 键：`process:{messageId}`（过程区）、`call:{toolCallId}`（调用行展开体）、
 * `reasoning:{messageId}:{partIndex}`（思考行）。Task 删除时 `clearTask` 整桶丢掉。
 */
export type DisclosureKey = `process:${string}` | `call:${string}` | `reasoning:${string}:${number}`;

export const processKey = (messageId: string): DisclosureKey => `process:${messageId}`;
export const callKey = (toolCallId: string): DisclosureKey => `call:${toolCallId}`;
export const reasoningKey = (messageId: string, partIndex: number): DisclosureKey => `reasoning:${messageId}:${partIndex}`;

type Overrides = Readonly<Record<string, boolean>>;

type DisclosureState = {
  /** taskId → 该 Task 内的用户覆盖。 */
  tasks: Readonly<Record<number, Overrides>>;
  set: (taskId: number, key: DisclosureKey, open: boolean) => void;
  /** 清掉某个键的覆盖，之后回到默认规则。 */
  reset: (taskId: number, key: DisclosureKey) => void;
  /** 看板「定位到调用」：把调用所在的过程区与该调用一起置为展开，再由调用方滚动。 */
  reveal: (taskId: number, toolCallId: string, messageId: string) => void;
  clearTask: (taskId: number) => void;
};

export const useDisclosureStore = create<DisclosureState>()((set) => ({
  tasks: {},
  set: (taskId, key, open) => set((state) => ({
    tasks: { ...state.tasks, [taskId]: { ...state.tasks[taskId], [key]: open } },
  })),
  reset: (taskId, key) => set((state) => {
    const bucket = state.tasks[taskId];
    if (!bucket || !(key in bucket)) return state;
    return { tasks: { ...state.tasks, [taskId]: without(bucket, key) } };
  }),
  reveal: (taskId, toolCallId, messageId) => set((state) => ({
    tasks: {
      ...state.tasks,
      [taskId]: { ...state.tasks[taskId], [processKey(messageId)]: true, [callKey(toolCallId)]: true },
    },
  })),
  clearTask: (taskId) => set((state) => (taskId in state.tasks ? { tasks: without(state.tasks, taskId) } : state)),
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
