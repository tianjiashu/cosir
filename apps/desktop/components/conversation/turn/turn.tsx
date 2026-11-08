import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { MessagePrimitive, useAuiState, useScrollLock } from "@assistant-ui/react";

import { CallRow } from "@/components/conversation/tools/call-row";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import type { TransportActivity, TransportOutcome } from "@/lib/assistant/contract";
import { STOPPING_FEEDBACK } from "@/lib/assistant/turn/current-activity";
import { readToolArtifact } from "@/lib/assistant/tools/tool-artifact";
import { AgentMessageRow } from "@/components/conversation/thread/agent-message";
import { visibleTurnBlock, type TurnLayout, type CallItem } from "@/lib/assistant/turn/turn-layout";
import { ArtifactZone } from "./artifact-zone";
import { AttentionCard } from "./attention-card";
import { TEXT_PART_COMPONENTS } from "./markdown-part";
import { processKey, useDisclosure } from "@/lib/assistant/turn/disclosure-store";
import { ProcessBlock } from "./process-block";
import { ActivityStatus } from "./thinking-label";
import { TurnError } from "./turn-error";
import { TurnFooter } from "./turn-footer";
import { LiveTurnHeading } from "./turn-heading";

export type TurnProps = {
  taskId: number;
  readonly: boolean;
  /** 用户已请求停止 Task 的当前活动、尚未收敛。 */
  cancelling: boolean;
  /** 放进回合脚注的回复级操作；回合不知道它是什么。 */
  footerAction?: ReactNode;
};

/** 调用行按 part 下标订阅 assistant-ui 的 part：拿到 args 与 artifact，同一调用从排队到终态始终是这一行。 */
const CallRowByIndex = memo(function CallRowByIndex({ index, taskId, interactive, taskCancelling, visible, inherited }: { index: number; taskId: number; interactive: boolean; taskCancelling: boolean; visible: boolean; inherited: boolean }) {
  const part = useAuiState((state) => state.message.parts[index]);
  const [mounted, setMounted] = useState(visible);
  if (visible && !mounted) setMounted(true);
  if (!mounted) return null;
  if (part?.type !== "tool-call") return null;
  return (
    <RenderIsolationBoundary
      scope="part"
      resetKey={`call:${part.toolCallId}`}
      metadata={{ partId: part.toolCallId, partIndex: index, partType: "tool-call", renderer: "call-row", toolName: part.toolName }}
      fallback={({ onRetry }) => <RenderErrorCard scope="tool" label={part.toolName} onRetry={onRetry} />}
    >
      <CallRow
        visible={visible}
        inherited={inherited}
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
});

const AttentionByIndex = memo(function AttentionByIndex({ index, taskId, interactive }: { index: number; taskId: number; interactive: boolean }) {
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
        args={(part.args ?? {}) as Record<string, unknown>}
        artifact={readToolArtifact(part.artifact)}
        interactive={interactive}
      />
    </RenderIsolationBoundary>
  );
});

/**
 * 助手消息按原始 part 顺序保留稳定槽位，工具区间摘要与正文交替。正常完成且有最终回复时隐藏过程，
 * 不移动正文 DOM；需要操作的卡与文件结果独立保留。布局由 projectTurn 给出，活动计时读取 startedAt，
 * 终态读取 outcome.durationMs，不创造挂起原因或猜测模型最终答复。
 */
export function Turn({ taskId, readonly, cancelling, footerAction }: TurnProps) {
  const messageId = useAuiState((state) => state.message.id);
  const custom = useAuiState((state) => state.message.metadata.custom);
  const turn = custom?.turn as number;
  const inherited = custom?.inherited === true;
  const outcome = (custom?.outcome ?? null) as TransportOutcome | null;
  const activity = (custom?.activity ?? null) as TransportActivity | null;
  const layout = custom.turnLayout as TurnLayout;
  const phase = layout.phase;
  const answerText = useMemo(() => layout.answer.map((item) => item.part.text).join("\n\n"), [layout.answer]);
  const interactive = !readonly && !inherited;
  const renderCall = useCallback((item: CallItem, visible: boolean) => <CallRowByIndex index={item.index} taskId={taskId} interactive={interactive} taskCancelling={cancelling} visible={visible} inherited={inherited} />, [taskId, interactive, cancelling, inherited]);

  const [processOpen, setProcessOpen] = useDisclosure(taskId, processKey(messageId), layout.defaultProcessOpen);
  const root = useRef<HTMLDivElement>(null);
  // 焦点不能留在隐藏元素上，交给标题，再按一次就能展开回去。过程收起和块失去常显条件（审批
  // 决定完、当前活动切走）都会隐藏元素，所以每次提交都查，不在焦点里时只有一次 contains。
  // 焦点事实只读 DOM：不用 React 状态镜像，获得焦点的按钮随工具结束卸载时收不到 blur。
  useLayoutEffect(() => {
    const active = document.activeElement;
    if (root.current?.contains(active) && active?.closest("[hidden]")) {
      root.current.querySelector<HTMLElement>('[data-slot="process-head"]')?.focus({ preventScroll: true });
    }
  });
  const lockScroll = useScrollLock(root, 0);
  const stopped = outcome?.status === "cancelled" || outcome?.status === "failed";

  return (
    <div ref={root}
      data-slot="turn" data-inherited={inherited || undefined} data-phase={phase} className="flex min-w-0 flex-col gap-4">
      <LiveTurnHeading layout={layout} outcome={outcome} startedAt={activity?.startedAt}
        open={processOpen} onToggle={() => { lockScroll(); setProcessOpen(!processOpen); }} />
      {layout.blocks.map((block) => {
        const hidden = !visibleTurnBlock(block, processOpen, layout.current?.groupKey);
        return <div key={block.kind === "activity" ? `activity:${block.key}` : `part:${block.index}`}
          hidden={hidden}
          data-slot={block.kind === "narration" ? "prose-part" : "process-part"} data-part-index={block.index} className="min-w-0">
          {block.kind === "narration" ? <div className="text-foreground text-chat leading-7">
            <MessagePrimitive.PartByIndex index={block.index} components={TEXT_PART_COMPONENTS} />
          </div> : block.kind === "agent" ? <AgentMessageRow id={block.part.id} text={block.part.text} source={block.part.source} taskId={taskId} />
            : block.kind === "standalone" ? <CallRowByIndex index={block.index} taskId={taskId} interactive={interactive} taskCancelling={cancelling} visible={!hidden} inherited={inherited} />
            : <ProcessBlock taskId={taskId} messageId={messageId} group={block} feedback={layout.current?.groupKey === block.key ? (cancelling ? STOPPING_FEEDBACK : layout.current.feedback) : undefined} visible={!hidden} renderCall={renderCall} />}
        </div>;
      })}
      {/* 输出正文时没有活动槽位，停止反馈不能跟着消失。 */}
      {cancelling && phase === "running" && !layout.current && <ActivityStatus feedback={STOPPING_FEEDBACK} className="block text-2sm" />}
      {stopped && <p data-slot="partial-reply" className="text-muted-foreground text-2sm">{answerText.trim() ? "回复可能尚未完成。" : "尚未生成回复。"}</p>}
      {layout.attention.length > 0 && <div data-slot="attention-zone" className="flex flex-col gap-2">
        {layout.attention.map((item) => <AttentionByIndex key={item.part.toolCallId} index={item.index} taskId={taskId} interactive={interactive} />)}
      </div>}
      <ArtifactZone inherited={inherited} taskId={taskId} artifacts={layout.artifacts} interactive={interactive} taskCancelling={cancelling} />
      <TurnError />
      {!layout.intro && <div>
        <TurnFooter
          taskId={taskId}
          messageId={messageId}
          turn={turn}
          inherited={inherited}
          outcome={outcome}
          activity={activity}
          answerText={answerText}
          hasFinalReply={layout.hasFinalReply}
          readonly={readonly}
          action={footerAction}
        />
      </div>}
    </div>
  );
}
