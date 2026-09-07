/** 创建任务后等待投递的初始消息。消息身份必须绑定到创建出的 task。 */
export type PendingInitialMessage = {
  taskId: number;
  text: string;
};

/**
 * 返回当前 task 可消费的初始消息。
 *
 * 参数:
 *   pending: 创建流程暂存的初始消息。
 *   taskId: 当前 Assistant runtime 对应的 task。
 * 返回:
 *   仅当消息属于当前 task 且文本非空时返回文本，否则返回 undefined。
 */
export function initialMessageForTask(
  pending: PendingInitialMessage | null,
  taskId: number,
): string | undefined {
  if (pending?.taskId !== taskId || !pending.text.trim()) return undefined;
  return pending.text;
}
