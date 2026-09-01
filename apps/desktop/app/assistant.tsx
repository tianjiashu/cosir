"use client";

import { Skeleton } from "@/components/ui/skeleton";
import { useAssistantInitialState } from "@/hooks/use-assistant-initial-state";
import { AssistantRuntime } from "@/components/assistant/assistant-runtime";

/**
 * 桌面端主入口：先拉取服务端首屏历史，state 就绪后再挂载 runtime。
 *
 * 关键约束（任务书 §3.5）：`useAssistantTransportRuntime` 只在 runtime 首次创建时
 * 捕获一次 `initialState`，之后无论如何变化都不再生效。因此本组件在挂载后立即
 * 经 `useAssistantInitialState` 拉取服务端 state，待数据就绪才渲染内层
 * `AssistantRuntime`；并用 `key={taskId}` 保证切换 task 时内层组件连同 runtime
 * 一并重建，避免历史残留。
 *
 * 本文件为瘦入口：只负责「拉取状态 → 三分支渲染（加载/错误/就绪）」，不再承载
 * 消息投递、runtime 装配等子职责（已拆至 components/assistant/ 下）。
 *
 * @param taskId - 当前任务 id。
 * @returns 加载中显示骨架屏；出错显示错误提示与「重试」按钮；就绪后挂载对话 runtime。
 */
export const Assistant = ({ taskId }: { taskId: number }) => {
  const { initialState, error, retry } = useAssistantInitialState(taskId);

  if (error) {
    return (
      <div className="flex h-dvh flex-col items-center justify-center gap-3 text-sm">
        <p className="text-destructive">{error}</p>
        <button
          type="button"
          className="text-muted-foreground underline underline-offset-4"
          onClick={retry}
        >
          重试
        </button>
      </div>
    );
  }

  if (!initialState) {
    return (
      <div className="flex h-dvh flex-col gap-3 p-5">
        <Skeleton className="h-16 w-2/3" />
        <Skeleton className="h-16 w-3/4" />
        <Skeleton className="h-16 w-1/2" />
      </div>
    );
  }

  return (
    // key 保证切换 task 时 runtime 整体重建（任务书 §3.5），避免历史交叉残留。
    <AssistantRuntime key={taskId} taskId={taskId} initialState={initialState} />
  );
};
