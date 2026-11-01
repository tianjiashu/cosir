import { useMemo } from "react";
import { MessagePrimitive, useAuiState } from "@assistant-ui/react";

import { CallRow } from "@/components/assistant-ui/tools/call-row";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import type { TransportActivity, TransportOutcome } from "@/lib/assistant/contract";
import { useElapsedSince } from "@/lib/assistant/run-duration";
import { transportPartsOf } from "@/lib/assistant/thread-message-parts";
import { readToolArtifact } from "@/lib/assistant/tool-artifact";
import { projectTurn, type CallItem, type TurnPhase } from "@/lib/assistant/turn-layout";
import { ArtifactZone } from "./artifact-zone";
import { AttentionCard } from "./attention-card";
import { TEXT_PART_COMPONENTS } from "./markdown-part";
import { ProcessBlock } from "./process-block";
import { TurnError } from "./turn-error";
import { TurnFooter } from "./turn-footer";

export type TurnProps = {
  taskId: number;
  readonly: boolean;
  /** 用户已请求停止 Task 的当前活动、尚未收敛。 */
  cancelling: boolean;
  forkingMessageId: string | null;
  onForkMessage?: (messageId: string) => void;
};

/**
 * 阶段由消息上下文给出：活动 Run 在运行 → `running`，挂起（等审批 / 交接）→ `suspended`，终态或不是本轮
 * 末尾的消息 → `settled`。`activity` 只挂在当前活动那一轮的回复末尾（见 frame store `contextFor`）。
 */
export function turnPhaseOf(activity: TransportActivity | null): TurnPhase {
  if (activity?.status === "running") return "running";
  if (activity?.status === "suspended") return "suspended";
  return "settled";
}

/** 调用行按 part 下标订阅 assistant-ui 的 part：拿到 args 与 artifact，同一调用从排队到终态始终是这一行。 */
function CallRowByIndex({ index, taskId, interactive, taskCancelling }: { index: number; taskId: number; interactive: boolean; taskCancelling: boolean }) {
  const part = useAuiState((state) => state.message.parts[index]);
  if (part?.type !== "tool-call") return null;
  return (
    <RenderIsolationBoundary
      scope="part"
      resetKey={`call:${part.toolCallId}`}
      metadata={{ partId: part.toolCallId, partIndex: index, partType: "tool-call", renderer: "call-row", toolName: part.toolName }}
      fallback={({ onRetry }) => <RenderErrorCard scope="tool" label={part.toolName} onRetry={onRetry} />}
    >
      <CallRow
        taskId={taskId}
        toolCallId={part.toolCallId}
        toolName={part.toolName}
        args={(part.args ?? {}) as Record<string, unknown>}
        artifact={readToolArtifact(part.artifact)}
        interactive={interactive}
        taskCancelling={taskCancelling}
      />
    </RenderIsolationBoundary>
  );
}

function AttentionByIndex({ index, taskId, interactive, taskCancelling }: { index: number; taskId: number; interactive: boolean; taskCancelling: boolean }) {
  const part = useAuiState((state) => state.message.parts[index]);
  if (part?.type !== "tool-call") return null;
  return (
    <RenderIsolationBoundary
      scope="part"
      resetKey={`attention:${part.toolCallId}`}
      metadata={{ partId: part.toolCallId, partIndex: index, partType: "tool-call", renderer: "attention-card", toolName: part.toolName }}
      fallback={({ onRetry }) => <RenderErrorCard scope="tool" label={part.toolName} onRetry={onRetry} />}
    >
      <AttentionCard
        taskId={taskId}
        toolCallId={part.toolCallId}
        toolName={part.toolName}
        artifact={readToolArtifact(part.artifact)}
        interactive={interactive}
        taskCancelling={taskCancelling}
      />
    </RenderIsolationBoundary>
  );
}

/**
 * 一个助手回合：过程（叙述、思考、调用） → 等待用户 → 本轮产物 → 结论 → 结局 → 脚注（设计 §5.1）。
 * 分区由 `projectTurn` 纯函数给出，这里只按阶段决定开合与计时：运行中按 `activity.startedAt` 本地计时，
 * 结束后读 `outcome.durationMs`。
 */
export function Turn({ taskId, readonly, cancelling, forkingMessageId, onForkMessage }: TurnProps) {
  const messageId = useAuiState((state) => state.message.id);
  const parts = useAuiState((state) => state.message.parts);
  const custom = useAuiState((state) => state.message.metadata.custom);
  const isRunEnd = custom?.isRunEnd === true;
  const runId = typeof custom?.runId === "number" ? custom.runId : null;
  const outcome = (custom?.outcome ?? null) as TransportOutcome | null;
  const activity = (custom?.activity ?? null) as TransportActivity | null;
  const phase = turnPhaseOf(activity);
  const layout = useMemo(() => projectTurn(transportPartsOf(parts), phase), [parts, phase]);
  const elapsed = useElapsedSince(activity?.startedAt, phase === "running");
  const durationMs = outcome?.durationMs ?? (phase === "settled" ? null : elapsed);
  const answerText = useMemo(() => layout.answer.map((item) => item.part.text).join("\n\n"), [layout.answer]);
  const interactive = !readonly;

  return (
    <div data-slot="turn" data-phase={phase} className="flex min-w-0 flex-col gap-3">
      {layout.process.items.length > 0 && (
        <ProcessBlock
          taskId={taskId}
          messageId={messageId}
          phase={phase}
          process={layout.process}
          durationMs={durationMs}
          renderCall={(item: CallItem) => <CallRowByIndex index={item.index} taskId={taskId} interactive={interactive} taskCancelling={cancelling} />}
        />
      )}
      {layout.attention.length > 0 && (
        <div data-slot="attention-zone" className="flex flex-col gap-2">
          {layout.attention.map((item) => (
            <AttentionByIndex key={item.part.toolCallId} index={item.index} taskId={taskId} interactive={interactive} taskCancelling={cancelling} />
          ))}
        </div>
      )}
      <ArtifactZone taskId={taskId} artifacts={layout.artifacts} interactive={interactive} taskCancelling={cancelling} />
      {layout.answer.length > 0 && (
        <div data-slot="answer" className="text-foreground flex flex-col gap-3 text-sm">
          {layout.answer.map((item) => <MessagePrimitive.PartByIndex key={item.index} index={item.index} components={TEXT_PART_COMPONENTS} />)}
        </div>
      )}
      <TurnError />
      <div>
        <TurnFooter
          taskId={taskId}
          messageId={messageId}
          runId={runId}
          isRunEnd={isRunEnd}
          outcome={outcome}
          activity={activity}
          answerText={answerText}
          readonly={readonly}
          forkingMessageId={forkingMessageId}
          onForkMessage={onForkMessage}
        />
      </div>
    </div>
  );
}
