"use client";

import { Thread } from "@/components/assistant-ui/elements/thread.aui";

/**
 * 在调用方 runtime 上以只读模式渲染 canonical Thread。
 *
 * 只读作用域必须复用外层 runtime：``ReadonlyThreadProvider`` 会换上一个只携带 messages 的
 * ``ReadonlyThreadRuntimeCore``，该 core 不承载 external state，使 ``thread.state`` 恒为
 * null；Thread 子树中读取 Transport state 的 selector 因此会在渲染期抛错，导致整屏渲染失败。
 */
export function ReadonlyThread({ taskId }: { taskId?: number }) {
  return <Thread readonly autoFocus={false} taskId={taskId} />;
}
