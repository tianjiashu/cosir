import { create } from "zustand";
import type { TransportActivity } from "./contract";
import type { TaskStatus } from "@/lib/task-status/task-status-store";

/** 一种操作一个槽位：同一调用的停止、转后台和决定是三件事，互不占用，后端的并发裁决不靠前端串行。 */
export type CommandTarget = "task" | `stop:${string}` | `background:${string}` | `decision:${string}`;
export type CommandRequest = {
  token: object;
  phase: "sending" | "syncing";
  error: string | null;
};
type Requests = Record<number, Partial<Record<CommandTarget, CommandRequest>>>;

/**
 * 跨界面的命令暂态，不是执行事实：同一命令在对话、看板、面板上共用一个「进行中」，切换视图不清除。
 *
 * 只有 Task 停止需要对账：它的按钮一直在，要知道何时不再显示「停止中」。前端看不到 Run，对账只看
 * 当前活动是否离开活动态；增量帧里停下与新 Run 开出总在不同帧，只有订阅断开重连拿到的完整快照会把
 * 二者并在一起，所以订阅断开时转为「待确认」（见 {@link markTaskRequestsUnsynced}）。工具调用级的按钮
 * 只在目标存活时渲染（存活由目标自身的事实决定：调用的 canonical、TeamRun 的轮询），所以它们的条目
 * 不用对账，调用 id 不复用，残留条目也不可见；按钮与失败提示的渲染条件里不能带槽位状态。
 */
export const useCommandRequests = create<{ tasks: Requests }>(() => ({ tasks: {} }));
const observed = new Map<number, TransportActivity | null>();

function stopSettled(activity: TransportActivity | null): boolean {
  return !(activity?.status === "running" || activity?.status === "suspended");
}

export const stopTarget = (id: string): CommandTarget => `stop:${id}`;
export const backgroundTarget = (id: string): CommandTarget => `background:${id}`;
export const decisionTarget = (id: string): CommandTarget => `decision:${id}`;

export function useCommandRequest(taskId: number, target: CommandTarget): CommandRequest | undefined {
  return useCommandRequests((state) => state.tasks[taskId]?.[target]);
}
/** 结果未知时可以重试：停止幂等，决定由后端判重（重复提交只会 409）。 */
export function commandPending(request: CommandRequest | undefined): boolean {
  return !!request && request.error === null;
}
export function reconcileTaskStatuses(tasks: Readonly<Record<number, TaskStatus>>): void {
  for (const [id, { executionStatus }] of Object.entries(tasks)) {
    if (executionStatus !== null && !["running", "suspended"].includes(executionStatus)) settleCommand(Number(id), "task");
  }
}
/**
 * 状态流或对话订阅断开时可能错过「停下、又开出新运行」的边界，不再声称正在停止：转为待确认、可重试。
 * 不传 `taskId` 时作用于全部 Task（应用级状态流断开）。
 */
export function markTaskRequestsUnsynced(taskId?: number): void {
  useCommandRequests.setState(state => {
    const tasks = { ...state.tasks };
    let changed = false;
    for (const [id, bucket] of Object.entries(tasks)) {
      if (taskId !== undefined && Number(id) !== taskId) continue;
      if (!bucket.task || bucket.task.error) continue;
      tasks[Number(id)] = {
        ...bucket,
        task: { ...bucket.task, phase: "syncing", error: "停止结果待确认，可手动重试停止。" },
      };
      changed = true;
    }
    return changed ? { tasks } : state;
  });
}
export function beginCommand(taskId: number, target: CommandTarget): CommandRequest | null {
  if (commandPending(useCommandRequests.getState().tasks[taskId]?.[target])) return null;
  const request: CommandRequest = { token: {}, phase: "sending", error: null };
  useCommandRequests.setState((state) => ({ tasks: { ...state.tasks, [taskId]: { ...state.tasks[taskId], [target]: request } } }));
  return request;
}
/** token 防止迟到的 HTTP 响应复活已被订阅收尾的请求。 */
export function finishCommand(taskId: number, target: CommandTarget, request: CommandRequest, result: "accepted" | "rejected" | "unknown", error: string | null = null): void {
  useCommandRequests.setState((state) => {
    const current = state.tasks[taskId]?.[target];
    if (current?.token !== request.token) return state;
    const bucket = { ...state.tasks[taskId] };
    const settled = target === "task" && observed.has(taskId) && stopSettled(observed.get(taskId) ?? null);
    if (result === "rejected" || settled) delete bucket[target];
    else bucket[target] = { ...current, phase: "syncing", error: error ?? current.error };
    return { tasks: { ...state.tasks, [taskId]: bucket } };
  });
}
function settleCommand(taskId: number, target: CommandTarget): void {
  useCommandRequests.setState((state) => {
    if (!state.tasks[taskId]?.[target]) return state;
    const bucket = { ...state.tasks[taskId] };
    delete bucket[target];
    return { tasks: { ...state.tasks, [taskId]: bucket } };
  });
}

/** canonical 发布时的唯一观察入口。 */
export function reconcileCommands(taskId: number, activity: TransportActivity | null): void {
  observed.set(taskId, activity);
  const request = useCommandRequests.getState().tasks[taskId]?.task;
  if (request && stopSettled(activity)) settleCommand(taskId, "task");
}

/** 这个 Task 不再被观察：离开页面后的旧快照不能再用来判定别处发起的停止。 */
export function releaseCommandObservation(taskId: number): void {
  observed.delete(taskId);
}
export function forgetCommands(taskIds: readonly number[]): void {
  useCommandRequests.setState((state) => {
    const tasks = { ...state.tasks };
    for (const id of taskIds) { delete tasks[id]; observed.delete(id); }
    return { tasks };
  });
}
