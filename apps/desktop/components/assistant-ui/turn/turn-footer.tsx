import { useEffect, useState } from "react";
import { CheckIcon, CopyIcon, GitForkIcon, Loader2Icon } from "lucide-react";
import { ActionBarPrimitive } from "@assistant-ui/react";

import { RunDiagnosticsActions } from "@/components/assistant/run-diagnostics-actions";
import { RunUsageDisplay } from "@/components/assistant/usage-display";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import type { TransportActivity, TransportOutcome } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";

export type TurnFooterProps = {
  taskId: number;
  messageId: string;
  runId: number | null;
  isRunEnd: boolean;
  outcome: TransportOutcome | null;
  activity: TransportActivity | null;
  /** 结论正文（答案区各正文 part 拼接）；复制只复制它，没有结论时复制不可用。 */
  answerText: string;
  readonly: boolean;
  forkingMessageId: string | null;
  onForkMessage?: (messageId: string) => void;
};

const COPIED_RESET_MS = 1_500;

/**
 * 回合脚注：用量 · 复制结论 · Fork · 诊断。用量、Fork 与诊断沿用原挂载条件（只在 Run 最后一条助手消息上）；
 * 复制不用 assistant-ui 的 `ActionBarPrimitive.Copy`（它拼接全部正文 part，会把中间叙述一起复制）。
 */
export function TurnFooter({ taskId, messageId, runId, isRunEnd, outcome, activity, answerText, readonly, forkingMessageId, onForkMessage }: TurnFooterProps) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), COPIED_RESET_MS);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const canFork = !readonly && isRunEnd && outcome !== null;
  const usage = outcome?.usage ?? activity?.usage ?? null;
  const status = outcome?.status ?? activity?.status ?? null;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(answerText);
      setCopied(true);
    } catch (error) {
      void frontendLog("WARNING", "turn_answer_copy_failed", "复制结论失败", { data: { taskId, messageId }, error });
    }
  };

  return (
    <>
      {!readonly && (
        <RunUsageDisplay usage={usage} running={activity !== null && outcome === null} visible={isRunEnd && (outcome !== null || activity !== null)} />
      )}
      <ActionBarPrimitive.Root className="mt-1 flex gap-1">
        <TooltipIconButton tooltip={copied ? "已复制" : "复制结论"} aria-label="复制结论" size="sm" disabled={answerText.trim() === ""} onClick={() => void copy()}>
          {copied ? <CheckIcon /> : <CopyIcon />}
        </TooltipIconButton>
        {!readonly && isRunEnd && (
          <TooltipIconButton
            tooltip={canFork ? "从此处 Fork 新任务" : "此轮结束后才能 Fork"}
            size="sm"
            aria-label="从此处 Fork 新任务"
            disabled={!canFork || forkingMessageId !== null}
            onClick={() => onForkMessage?.(messageId)}
          >
            {forkingMessageId === messageId ? <Loader2Icon className="animate-spin" /> : <GitForkIcon />}
          </TooltipIconButton>
        )}
        <RunDiagnosticsActions
          taskId={taskId}
          runId={runId}
          status={status}
          endReason={outcome?.endReason ?? null}
          errorCode={outcome?.error?.code ?? null}
          visible={isRunEnd}
        />
      </ActionBarPrimitive.Root>
    </>
  );
}
