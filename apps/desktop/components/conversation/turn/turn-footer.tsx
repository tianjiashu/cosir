import type { ReactNode } from "react";
import { ActionBarPrimitive, useAuiState } from "@assistant-ui/react";

import { TurnDiagnosticsActions } from "@/components/conversation-runtime/turn-diagnostics-actions";
import { RunUsageDisplay } from "@/components/conversation-runtime/usage-display";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { REVEAL_ON_MESSAGE_HOVER } from "@/components/ui/reveal-on-hover";
import type { TransportActivity, TransportOutcome } from "@/lib/assistant/contract";
import { cn } from "@/lib/utils";

export type TurnFooterProps = {
  taskId: number;
  messageId: string;
  turn: number;
  inherited: boolean;
  outcome: TransportOutcome | null;
  activity: TransportActivity | null;
  /** 按消息结局选择的可读正文；无正文时复制不可用。 */
  answerText: string;
  hasFinalReply: boolean;
  readonly: boolean;
  /** 线程提供的回复级操作，由它自己判断是否可用。 */
  action?: ReactNode;
};

/**
 * 回合脚注：复制回复 · 回复级操作 · 诊断 · 用量，一行。复制不用 assistant-ui 的 `ActionBarPrimitive.Copy`（它拼接
 * 全部正文 part，会把中间叙述一起复制）。用量只在带 Run 结局或当前活动的回复上（结局属于整个 Run，挂在它最后
 * 一个可见回合的回复上）；不放进回合标题，因为只有正文的回复没有标题。
 *
 * 脚注是次要操作：最后一条回复常显（运行中的回复就是最后一条，「用量统计中」照常可见），其余悬停或键盘聚焦
 * 这条消息（`group/message`）才出现，弹层打开期间保持可见。只改透明度、不卸载：assistant-ui 的 `autohide`
 * 隐藏时返回 null，悬停会让行高跳动，也会打破估高里按常驻计的 `ASSISTANT_CHROME`。
 */
export function TurnFooter({ taskId, messageId, turn, inherited, outcome, activity, answerText, hasFinalReply, readonly, action }: TurnFooterProps) {
  const usage = outcome?.usage ?? activity?.usage ?? null;
  const status = outcome?.status ?? activity?.status ?? null;
  const isLast = useAuiState((state) => state.message.isLast);

  return (
    <ActionBarPrimitive.Root
      data-slot="turn-footer"
      className={cn("flex items-center gap-1 transition-opacity", !isLast && REVEAL_ON_MESSAGE_HOVER, "has-[[data-popup-open]]:opacity-100")}
    >
      <CopyIconButton
        label={hasFinalReply ? "复制回复" : "复制已生成内容"}
        size="sm"
        disabled={answerText.trim() === ""}
        getText={() => answerText}
        logEvent="turn_answer_copy_failed"
        logData={{ taskId, messageId }}
      />
      {action}
      <TurnDiagnosticsActions
        taskId={taskId}
        turn={turn}
        inherited={inherited}
        status={status}
        endReason={outcome?.endReason ?? null}
        errorCode={outcome?.error?.code ?? null}
      />
      {!readonly && (
        <RunUsageDisplay usage={usage} running={activity !== null && outcome === null} visible={outcome !== null || activity !== null} />
      )}
    </ActionBarPrimitive.Root>
  );
}
