import { useMemo } from "react";
import { useAuiState } from "@assistant-ui/react";

import { AgentConfigurationDraftTool } from "@/components/assistant-ui/tools/agent-configuration-draft-tool";
import { AgentTeamConfigurationTool } from "@/components/assistant-ui/tools/agent-team-configuration-tool";
import { AgentTeamTool } from "@/components/assistant-ui/tools/agent-team-tool";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import { readToolArtifact } from "@/lib/assistant/tool-artifact";
import type { ArtifactCard, StandaloneArtifactCard } from "@/lib/assistant/turn-layout";
import { ChangesCard } from "./changes-card";

export type ArtifactZoneProps = {
  taskId: number;
  artifacts: readonly ArtifactCard[];
  interactive: boolean;
  taskCancelling: boolean;
};

/**
 * 聚合卡要读参与调用的展示数据（点开文件时找 diff）。selector 只取 parts 数组本身（引用稳定），派生在
 * useMemo 里做：selector 每次返回新数组会让 useSyncExternalStore 不断重渲染。
 */
function ChangesArtifact({ card }: { card: Extract<ArtifactCard, { kind: "changes" }> }) {
  const parts = useAuiState((state) => state.message.parts);
  const displays = useMemo(() => card.callIndexes.map((index) => {
    const part = parts[index];
    return part?.type === "tool-call" ? readToolArtifact(part.artifact).display_data : null;
  }), [card.callIndexes, parts]);
  return <ChangesCard card={card} displays={displays} />;
}

/**
 * 草稿卡与 Team 卡：各自读自己那次调用的 artifact。Team 审批待确认（`awaitingUser`）时由等待卡承载表单，
 * 这里跳过，避免同一张卡出现两次；批准后（执行中 / 已结束 / 已驳回）才在产物区常显。
 */
function StandaloneArtifact({ card, taskId, interactive, taskCancelling }: { card: StandaloneArtifactCard; taskId: number; interactive: boolean; taskCancelling: boolean }) {
  const part = useAuiState((state) => state.message.parts[card.index]);
  if (part?.type !== "tool-call") return null;
  const artifact = readToolArtifact(part.artifact);
  if (artifact.awaitingUser) return null;
  const props = { taskId, toolCallId: card.part.toolCallId, artifact, interactive, taskCancelling };
  switch (card.kind) {
    case "agent-configuration-draft":
      return <AgentConfigurationDraftTool {...props} />;
    case "agent-team-configuration-draft":
      return <AgentTeamConfigurationTool {...props} />;
    case "agent-team-approval":
      return <AgentTeamTool {...props} />;
  }
}

/** 产物区：本轮改动（聚合）排在前，其余草稿卡 / Team 卡按调用顺序；每张卡各自隔离渲染错误。 */
export function ArtifactZone({ taskId, artifacts, interactive, taskCancelling }: ArtifactZoneProps) {
  if (artifacts.length === 0) return null;
  return (
    <div data-slot="artifact-zone" className="flex flex-col gap-2">
      {artifacts.map((card) => (card.kind === "changes" ? (
        <RenderIsolationBoundary
          key="changes"
          scope="part"
          resetKey={`changes:${card.callIndexes.join(",")}`}
          metadata={{ renderer: "changes-card", partIndices: card.callIndexes }}
          fallback={({ onRetry }) => <RenderErrorCard scope="part" label="本轮改动" onRetry={onRetry} />}
        >
          <ChangesArtifact card={card} />
        </RenderIsolationBoundary>
      ) : (
        <RenderIsolationBoundary
          key={card.part.toolCallId}
          scope="part"
          resetKey={`artifact:${card.part.toolCallId}`}
          metadata={{ renderer: card.kind, partId: card.part.toolCallId }}
          fallback={({ onRetry }) => <RenderErrorCard scope="tool" label={card.part.toolName} onRetry={onRetry} />}
        >
          <StandaloneArtifact card={card} taskId={taskId} interactive={interactive} taskCancelling={taskCancelling} />
        </RenderIsolationBoundary>
      )))}
    </div>
  );
}
