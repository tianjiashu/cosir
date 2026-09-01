"use client";

import { Button } from "@/components/ui/button";
import { useAssistantTransportState } from "@assistant-ui/react";
import { SquareIcon } from "lucide-react";
import { type FC } from "react";
import { cancelRun } from "@/lib/assistant/cancel-run";

/**
 * 「停止生成」按钮：在 assistant-ui 默认 abort（断开前端连接）之外，额外向服务端
 * 发起真实取消请求，把 turn 落定为 cancelled，避免取消被记为 failed/client_disconnected。
 *
 * @param taskId - 当前任务标识；仅用于取消请求的错误日志上下文。允许为 null，表示
 *   任务未知——此时如实写入日志（不伪造为 0），取消逻辑本身不依赖该值（路径由 turnId 决定）。
 * @returns 渲染一个图标按钮；点击时若 turnId 已知则发起真实取消请求，否则仅依赖默认 abort。
 * @remarks turnId 来自 assistant-ui 运行时透传的服务端 state（TransportState.run.runId），
 *   通过官方 `useAssistantTransportState` 读取；turnId 为 null 时退化为默认 abort 行为，
 *   不发取消请求。
 */
export const StopButton: FC<{ taskId: number | null }> = ({ taskId }) => {
  // 通过官方途径读取当前运行切片标识（后端 turns.id）：
  // 已对 @assistant-ui/react 的 Assistant.ExternalState 做模块增强（见
  // lib/assistant/assistant-external-state.d.ts），将 transportState 注入为
  // TransportState，故 selector 入参 s 已自动推导为 TransportState，无需 as 断言。
  // 其 run.runId 即为可取消的 turn 标识。无运行时为 null。
  const turnId = useAssistantTransportState((s) => s.run?.runId ?? null);

  return (
    <Button
      type="button"
      variant="default"
      size="icon"
      className="aui-composer-cancel size-7 rounded-full"
      aria-label="Stop generating"
      onClick={() => {
        // turnId 为 null 表示当前无运行中的 turn，仅依赖默认 abort 行为。
        if (turnId != null) {
          void cancelRun(taskId, turnId);
        }
      }}
    >
      <SquareIcon className="aui-composer-cancel-icon size-3.5 fill-current" />
    </Button>
  );
};
