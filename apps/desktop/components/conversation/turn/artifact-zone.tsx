import { memo } from "react";
import { useAuiState } from "@assistant-ui/react";

import { AgentConfigurationDraftTool } from "@/components/conversation/tools/configuration-draft/agent-configuration-draft-tool";
import { RenderErrorCard } from "@/components/render-isolation/render-error-card";
import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import { readToolArtifact } from "@/lib/assistant/tools/tool-artifact";
import type { ArtifactCard, StandaloneArtifactCard } from "@/lib/assistant/turn/turn-layout";
import { ChangesCard } from "./changes-card";

export type ArtifactZoneProps = {
  inherited?: boolean;
  sessionId: number;
  artifacts: readonly ArtifactCard[];
  interactive: boolean;
  sessionCancelling: boolean;
};

/** 文件汇总直接消费投影里的操作记录，不再订阅 parts 或按下标回查载荷。 */
function ChangesArtifact({ card, sessionId }: { card: Extract<ArtifactCard, { kind: "changes" }>; sessionId: number }) {
  const messageId = useAuiState((state) => state.message.id);
  return <ChangesCard card={card} sessionId={sessionId} messageId={messageId} />;
}

/**
 * 配置草稿卡：各自读自己那次调用的 artifact；等用户决定期间不显示。
 */
function StandaloneArtifact({ card, sessionId, interactive, sessionCancelling, inherited }: { inherited?: boolean; card: StandaloneArtifactCard; sessionId: number; interactive: boolean; sessionCancelling: boolean }) {
  const part = useAuiState((state) => state.message.parts[card.index]);
  if (part?.type !== "tool-call") return null;
  const artifact = readToolArtifact(part.artifact);
  if (artifact.awaitingUser) return null;
  const props = { inherited, sessionId, toolCallId: card.part.toolCallId, artifact, interactive, sessionCancelling };
  switch (card.kind) {
    case "agent-configuration-draft":
      return <AgentConfigurationDraftTool {...props} />;
  }
}

/** 产物区：本轮改动（聚合）排在前，其余草稿卡按调用顺序；每张卡各自隔离渲染错误。 */
export const ArtifactZone = memo(function ArtifactZone({ inherited, sessionId, artifacts, interactive, sessionCancelling }: ArtifactZoneProps) {
  if (artifacts.length === 0) return null;
  return (
    <div data-slot="artifact-zone" className="flex flex-col gap-2">
      {artifacts.map((card) => (card.kind === "changes" ? (
        <RenderIsolationBoundary
          key="changes"
          scope="part"
          resetKey={`changes:${card.callIndexes.join(",")}`}
          metadata={{ renderer: "changes-card", partIndices: card.callIndexes }}
          fallback={({ onRetry }) => <RenderErrorCard scope="part" label="文件操作记录" onRetry={onRetry} />}
        >
          <ChangesArtifact card={card} sessionId={sessionId} />
        </RenderIsolationBoundary>
      ) : (
        <RenderIsolationBoundary
          key={card.part.toolCallId}
          scope="part"
          resetKey={`artifact:${card.part.toolCallId}`}
          metadata={{ renderer: card.kind, partId: card.part.toolCallId }}
          fallback={({ onRetry }) => <RenderErrorCard scope="tool" label={card.part.toolName} onRetry={onRetry} />}
        >
          <StandaloneArtifact inherited={inherited} card={card} sessionId={sessionId} interactive={interactive} sessionCancelling={sessionCancelling} />
        </RenderIsolationBoundary>
      )))}
    </div>
  );
});
