import type { ReactNode } from "react";
import { ActionBarPrimitive } from "@assistant-ui/react";

import { TurnDiagnosticsActions } from "@/components/conversation-runtime/turn-diagnostics-actions";
import { RunUsageDisplay } from "@/components/conversation-runtime/usage-display";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import type { TransportActivity, TransportOutcome } from "@/lib/assistant/contract";

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
 * 回合脚注：用量 · 复制回复 · 回复级操作 · 诊断。用量只在带 Run 结局或当前活动的回复上（结局属于整个
 * Run，挂在它最后一个可见回合的回复上）；复制不用 assistant-ui 的 `ActionBarPrimitive.Copy`（它拼接
 * 全部正文 part，会把中间叙述一起复制）。
 */
export function TurnFooter({ taskId, messageId, turn, inherited, outcome, activity, answerText, hasFinalReply, readonly, action }: TurnFooterProps) {
  const usage = outcome?.usage ?? activity?.usage ?? null;
  const status = outcome?.status ?? activity?.status ?? null;

  return (
    <>
      {!readonly && (
        <RunUsageDisplay usage={usage} running={activity !== null && outcome === null} visible={outcome !== null || activity !== null} />
      )}
      <ActionBarPrimitive.Root className="mt-1 flex gap-1">
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
      </ActionBarPrimitive.Root>
    </>
  );
}
